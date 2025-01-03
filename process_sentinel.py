import os, glob
import rasterio
import numpy as np
import xmltodict
import boto3
import json
import logging
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from zipfile import ZipFile
from pyproj import Transformer
from shapely.ops import transform
from shapely.geometry import shape
from rasterio.mask import mask
from shapely.wkt import loads
from sqlalchemy import text
from osgeo import gdal
from datetime import datetime
from dotenv import load_dotenv
from config import Config
from apps.models import db

gdal.UseExceptions()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ProcessSentinel:
    def __init__(self, local_rawfile_path: str, aoi: dict):
        self.local_rawfile_path = local_rawfile_path
        self.aoi = aoi

        load_dotenv()

        self.s3_client = boto3.client(
            "s3", 
            aws_access_key_id=Config.AWS_ACCESS_KEY_ID, 
            aws_secret_access_key=Config.AWS_SECRET_ACCESS_KEY, 
            region_name=Config.AWS_DEFAULT_REGION
        )
        self.AWS_BUCKET_NAME = Config.AWS_BUCKET

        self.temp_dir = os.path.join(Config.LOCAL_STORAGE_FOLDER, 'temp')
        os.makedirs(self.temp_dir, exist_ok=True)

    def process(self):
        try:
            try:
                logger.info(f'Extracting sentinel image...')
                if self.local_rawfile_path.endswith(".zip"):
                    with ZipFile(self.local_rawfile_path, 'r') as zip_ref:
                        zip_ref.extractall(self.temp_dir)
                        extracted_dir_name = zip_ref.namelist()[0].split('/')[0]
                        extracted_path = os.path.abspath(os.path.join(self.temp_dir, extracted_dir_name))
            except Exception as e:
                logger.error(f"Error when extracting zip file: {e}")
                return

            try:
                logger.info(f'Transforming sentinel image to NDVI and NBR...')
                ndvi_result, nbr_result = self.transform_image(extracted_path)
            except Exception as e:
                logger.error(f"Error when transforming image: {e}")
                return
            
            try:
                logger.info(f'Converting TIF to PNG..')
                ndvi_png_path = self.convert_tif_to_png(ndvi_result['path'])
                nbr_png_path = self.convert_tif_to_png(nbr_result['path'])
            except Exception as e:
                logger.error(f"Error when converting TIF to PNG: {e}")
                return

            try:
                year = ndvi_result['metadata']['DATE_ACQUIRED'].split("-")[0]

                logger.info(f'Saving NDVI image to S3...')
                ndvi_object_path = f"sentinel_ndvi/{self.aoi['pilot_name'].upper()}/{year}/{os.path.basename(ndvi_result['path'])}"
                ndvi_png_object_path = f"sentinel_ndvi/{self.aoi['pilot_name'].upper()}/{year}/{os.path.basename(ndvi_png_path)}"
                self.save_image(ndvi_result['path'], ndvi_object_path)
                self.save_image(ndvi_png_path, ndvi_png_object_path)

                ndvi_result['object_path'] = ndvi_object_path
                ndvi_result['stage'] = 'original'

                logger.info(f'Saving NBR image to S3...')
                nbr_object_path = f"sentinel_nbr/{self.aoi['pilot_name'].upper()}/{year}/{os.path.basename(nbr_result['path'])}"
                nbr_png_object_path = f"sentinel_nbr/{self.aoi['pilot_name'].upper()}/{year}/{os.path.basename(nbr_png_path)}"
                self.save_image(nbr_result['path'], nbr_object_path)
                self.save_image(nbr_png_path, nbr_png_object_path)
                
                nbr_result['object_path'] = nbr_object_path
                nbr_result['stage'] = 'original'
            except Exception as e:
                logger.error(f"Error when saving image: {e}")
                return
        
            try:
                logger.info(f'Saving stat value to DB...')
                self.save_stat_value(ndvi_result)
                self.save_stat_value(nbr_result)
            except Exception as e:
                logger.error(f"Error when saving stat value: {e}")
                return
            
            try:
                for aoi in self.aoi['geometries']:
                    is_intersected = db.session.execute(text("""
                    SELECT 1 WHERE ST_Intersects(
                        (SELECT pilot_geom FROM pilots WHERE pilot_id = 4),
                        (SELECT entity_geom FROM regional_entities WHERE entity_code = 'IDN')
                    )"""), {'geom': aoi['geom'], 'wkt_coords': ndvi_result['metadata']['WKT_COORDS']}).fetchone()
                    if not is_intersected:
                        logger.info(f'Geometry {aoi["geometry_id"]} is not intersected with the image')
                        continue

                    logger.info(f'Cropping image based on geometry: {aoi["geometry_id"]}')
                    cropped_ndvi_stat_value = self.crop_image(ndvi_result['path'], aoi['geom'])
                    cropped_nbr_stat_value = self.crop_image(nbr_result['path'], aoi['geom'])

                    cropped_ndvi_result = ndvi_result.copy()
                    cropped_ndvi_result['metadata']['WKT_COORDS'] = aoi['geom']
                    cropped_ndvi_result['metadata']['geometry_id'] = aoi['geometry_id']
                    cropped_ndvi_result['stat_value'] = cropped_ndvi_stat_value
                    cropped_ndvi_result['stage'] = 'cropped'

                    cropped_nbr_result = nbr_result.copy()
                    cropped_nbr_result['metadata']['WKT_COORDS'] = aoi['geom']
                    cropped_nbr_result['metadata']['geometry_id'] = aoi['geometry_id']
                    cropped_nbr_result['stat_value'] = cropped_nbr_stat_value
                    cropped_nbr_result['stage'] = 'cropped'

                    try:
                        logger.info(f'Saving cropped NDVI and NBR stat value to DB')
                        self.save_stat_value(cropped_ndvi_result)
                        self.save_stat_value(cropped_nbr_result)
                    except Exception as e:
                        logger.error(f"Error when saving cropped stat value: {e}")
                        return
                        
            except Exception as e:
                logger.error(f"Error when cropping image: {e}")
                return

        except Exception as e:
            logger.error(f"Error: {e}")
            return
        
        logger.info(f"{ndvi_result['metadata']['PRODUCT_NAME_ID']} has been processed successfully")
        return
    

    def convert_tif_to_png(self, raster_path):
        dirpath = os.path.dirname(raster_path)
        filename = os.path.basename(raster_path)
        base, ext = os.path.splitext(filename)
        reprojected_path = os.path.join(dirpath, f"{base}_reprojected{ext}")
        output_path = os.path.join(dirpath, f"{base}.png")

        ds = gdal.Open(raster_path)
        reprojected_ds = gdal.Warp(reprojected_path, ds, dstSRS='EPSG:3857', cropToCutline=True, dstNodata=np.nan)
        array = reprojected_ds.GetRasterBand(1).ReadAsArray()
        ds = reprojected_ds = None

        min_val = np.nanmin(array)
        max_val = np.nanmax(array)

        fig, ax = plt.subplots()
        ax.imshow(array, cmap='gray', vmin=min_val, vmax=max_val)
        ax.axis('off')
        ax.xaxis.set_major_locator(ticker.NullLocator())
        ax.yaxis.set_major_locator(ticker.NullLocator())
        ax.set_xlim(0, array.shape[1])
        ax.set_ylim(array.shape[0], 0)
        fig.patch.set_alpha(0)
        ax.patch.set_alpha(0)
        fig.subplots_adjust(top=1, bottom=0, right=1, left=0, hspace=0, wspace=0)
        ax.margins(0, 0)
        fig.savefig(output_path, dpi=300, bbox_inches='tight', pad_inches=0, transparent=True)
        plt.close()

        return output_path
    

    def transform_image(self, sentinel_dir):
        def search_required_files(root_path):
            image_list = [os.path.join(root, name) for root, dirs, files in os.walk(root_path) for name in files if name.endswith((".jp2", ".tif")) and "IMG_DATA" in root]
            metadata = [os.path.join(root, name) for root, dirs, files in os.walk(root_path) for name in files if name.endswith((".xml")) ]
            return image_list, metadata
        
        def raster_resample(raster_path, srs):
            data = glob.glob(raster_path)
            resampled_filename = "/"+os.path.join(*raster_path.split("/")[:-1], "resampled_"+raster_path.split("/")[-1])
            reproj = gdal.Warp(resampled_filename, data, dstSRS=srs, xRes=10, yRes=-10, resampleAlg='near', dstNodata=None)
            reproj = None
            return resampled_filename
        
        def band_check(raster_obj, raster_path):
            x_resolution = raster_obj.res[0]
            y_resolution = raster_obj.res[1]
            target_resolution = 10.0
            if x_resolution > target_resolution or y_resolution > target_resolution:
                resampled_raster_path = raster_resample(raster_path=raster_path, srs=raster_obj.crs)
                raster_obj = rasterio.open(resampled_raster_path)
            return raster_obj 
        
        def extract_metadata_sentinel(metafile):
            with open(metafile) as xml_file:
                data_dict = xmltodict.parse(xml_file.read())
                try:
                    json_data = data_dict["n1:Level-1C_User_Product"]
                except:
                    json_data = data_dict["n1:Level-2A_User_Product"]

            if json_data["n1:General_Info"].get("Product_Info"):
                gen_info = json_data["n1:General_Info"]["Product_Info"]
                product_name_id = gen_info["PRODUCT_URI"].replace(".SAFE", "")

            else:
                gen_info = json_data["n1:General_Info"]["L2A_Product_Info"]
                product_name_id = gen_info["PRODUCT_URI_2A"].replace(".SAFE", "")

            footprint = json_data["n1:Geometric_Info"]["Product_Footprint"]["Product_Footprint"]["Global_Footprint"]["EXT_POS_LIST"]
            footprint = footprint.split(" ")
            footprint_len = len(footprint)
            coords = []
            for i in range(0,footprint_len, 2):
                coord = {"lat": footprint[i], "lon": footprint[i+1]}
                coords.append(coord)

            wkt_coords = [f"{(coord['lon'])} {(coord['lat'])}" for coord in coords]
            wkt = f"POLYGON ({tuple(wkt_coords)})".replace('\'', '')

            desc = {
                "PRODUCT_NAME_ID": product_name_id,
                "DATE_ACQUIRED": datetime.strftime(datetime.strptime(gen_info["PRODUCT_START_TIME"].split(".")[0], "%Y-%m-%dT%H:%M:%S"), "%Y-%m-%d"),
                "SPACECRAFT_ID": gen_info["Datatake"]["SPACECRAFT_NAME"],
                "WKT_COORDS": wkt
            }
            return desc
    
        def stat_value(val):
            val_max = np.nanmax(val)
            val_min = np.nanmin(val)
            val_mean = np.nanmean(val)
            val_med = np.nanmedian(val)
            
            collect = {
                "max": float(val_max),
                "min": float(val_min),
                "mean": float(val_mean),
                "med": float(val_med),
            }
            
            return collect
        
        def get_percentage(val_ndvi):
            NDVI = val_ndvi
            
            NDVI_percentage = (NDVI.size / NDVI.size) * 100
            NDVI_percentage = round(NDVI_percentage, 2)
            
            very_poor = NDVI[np.where(NDVI<=0)]
            very_poor_percentage = (very_poor.size / NDVI.size) * 100
            very_poor_percentage = round(very_poor_percentage, 2)
            
            poor = NDVI[np.where(np.logical_and(NDVI > 0, NDVI <= 0.33))]
            poor_percentage = (poor.size / NDVI.size) * 100
            poor_percentage = round(poor_percentage,2)
            
            good = NDVI[np.where(np.logical_and(NDVI > 0.33, NDVI <= 0.66))]
            good_percentage = (good.size / NDVI.size) * 100
            good_percentage = round(good_percentage,2)
            
            very_good = NDVI[np.where(NDVI > 0.66)]
            very_good_percentage = (very_good.size / NDVI.size) * 100
            very_good_percentage = round(very_good_percentage,2)
            
            collect = {
                "total": {"value": NDVI.size, "percentage": NDVI_percentage},
                "very_poor": {"value": very_poor.size, "percentage": very_poor_percentage},
                "poor": {"value": poor.size, "percentage": poor_percentage},
                "good": {"value": good.size, "percentage": good_percentage},
                "very_good": {"value": very_good.size, "percentage": very_good_percentage}
            }
            return collect

        def export_index(raster_path, ref_raster, input_index, tipe, source="api", directory_path=""):
            if source.lower() == "api":
                result_path = os.path.join(directory_path)
            elif source.lower() == "app":
                result_path = os.path.join(raster_path)

            if not os.path.exists(result_path):
                os.mkdir(result_path)
            
            if tipe.lower() == "ndvi":
                tipe = "_NDVI.TIF"
            elif tipe.lower() == "fcd":
                tipe = "_FCD.TIF"
            elif tipe.lower() == "nbr":
                tipe = "_NBR.TIF"
            elif tipe.lower() == "dnbr":
                tipe = "_dNBR.TIF"
            elif tipe.lower() == "avi":
                tipe = "_AVI.TIF"
            elif tipe.lower() == "bsi":
                tipe = "_BSI.TIF"
            elif tipe.lower() == "si":
                tipe = "_SI.TIF"
            elif tipe.lower() == "ssi":
                tipe = "_SSI.TIF"
            elif tipe.lower() == "svd":
                tipe = "_SVD.TIF"

            raster_path = os.path.join(result_path, raster_path+tipe)
            with rasterio.open(raster_path, 'w', driver='Gtiff',
                                width=ref_raster.width,
                                height=ref_raster.height,
                                count=1,
                                crs=ref_raster.crs,
                                transform=ref_raster.transform,
                                dtype='float32') as SaveRasterImage:
                SaveRasterImage.write_band(1,input_index.astype(rasterio.float32))
            
            data = glob.glob(raster_path)
            warp = gdal.Warp(raster_path, data, dstSRS="EPSG:32749", resampleAlg='cubic')
            warp = None
            
            return raster_path

        def calculate_mapper_result(mapper_func, **kwargs):
            mapper_kwargs = {}
            for key, value in kwargs.items():
                if isinstance(value, str):
                    for entry in kwargs["image_list"]:
                        if value in entry:
                            mapper_kwargs[key] = entry
                            break
                if key == "mtl_entry":
                    for entry in kwargs["metadata"]:
                        if value in entry:
                            mapper_kwargs[key] = entry
            return mapper_func(directory_path=self.temp_dir, **mapper_kwargs)
        
        def calculate_ndvi(red_entry, nir_entry, mtl_entry, source="api", directory_path=""):
            red_band = band_check(rasterio.open(red_entry), red_entry)
            nir_band = band_check(rasterio.open(nir_entry), nir_entry)
            extracted_metadata = extract_metadata_sentinel(mtl_entry)

            np.seterr(divide='ignore', invalid='ignore')
            
            red = red_band.read(1).astype('float32')
            nir = nir_band.read(1).astype('float32')
            
            red[red==np.nan] = 0
            nir[nir==np.nan] = 0
            
            NDVI = np.divide(np.subtract(nir, red), np.add(nir, red))
            NDVI[np.isnan(NDVI)] = 0
            
            NDVI[NDVI<-1] = np.nan
            NDVI[NDVI>1] = np.nan
            
            ndvi_percentage = get_percentage(NDVI)
            ndvi_stat_value = stat_value(NDVI)
            
            NDVI_filename = extracted_metadata['PRODUCT_NAME_ID']
            raster_res = export_index(
                raster_path=NDVI_filename, 
                ref_raster=nir_band, 
                input_index=NDVI, 
                tipe="ndvi", 
                source=source, 
                directory_path=directory_path
            )
        
            result = {
                "metadata": extracted_metadata,
                "path": raster_res,
                "status": True,
                "type": "NDVI",
                "percentage": ndvi_percentage,
                "stat_value": ndvi_stat_value
            }
            
            return result
        
        def calculate_nbr(swir_entry, nir_entry, mtl_entry, source="api", directory_path=""):
            swir_band = band_check(rasterio.open(swir_entry), swir_entry)
            nir_band = band_check(rasterio.open(nir_entry), nir_entry)
            extracted_metadata = extract_metadata_sentinel(mtl_entry)
            
            np.seterr(divide='ignore', invalid='ignore')
            
            swir = swir_band.read(1).astype('float32')
            nir = nir_band.read(1).astype('float32')
            
            swir[swir==np.nan] = 0
            nir[nir==np.nan] = 0
            
            NBR = np.divide(np.subtract(nir, swir), np.add(nir, swir))
            NBR[np.isnan(NBR)] = 0
            
            NBR[NBR<-1] = np.nan
            NBR[NBR>1] = np.nan
            
            nbr_stat_value = stat_value(NBR)
                        
            NBR_filename = extracted_metadata['PRODUCT_NAME_ID']
            raster_res = export_index(raster_path=NBR_filename, ref_raster=nir_band, input_index=NBR, tipe="nbr", source=source, directory_path=directory_path) #Save NBR Image
            
            result = {
                "metadata": extracted_metadata,
                "path": raster_res,
                "status": True,
                "type": "NBR",
                "stat_value": nbr_stat_value
            }
            return result

        def get_ndvi_result(image_list, metadata):
               return calculate_mapper_result(
                calculate_ndvi,
                red_entry="B04",
                nir_entry="B08",
                mtl_entry="MTD_MSIL",
                image_list=image_list,
                metadata=metadata,
            )
        
        def get_nbr_result(image_list, metadata):
            return calculate_mapper_result(
                calculate_nbr,
                nir_entry="B08",
                swir_entry="B12",
                mtl_entry="MTD_MSIL",
                image_list=image_list,
                metadata=metadata,
            )
        
        image_list, metadata = search_required_files(sentinel_dir)
        ndvi_result = get_ndvi_result(image_list, metadata)
        nbr_result = get_nbr_result(image_list, metadata)
        return ndvi_result, nbr_result
    

    def crop_image(self, input_path: str, wkt_string: str):
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_cropped{ext}"

        def get_stat_values(data: np.ndarray, conversion=lambda x: x) -> dict:
            data = np.ma.filled(data, np.nan)
            min_val = np.nanmin(data)
            max_val = np.nanmax(data)
            mean_val = np.nanmean(data)
            median_val = np.nanmedian(data)
            return {"max": conversion(max_val), "min": conversion(min_val), "mean": conversion(mean_val), "med": conversion(median_val)}

        with rasterio.open(input_path) as src:
            wkt_object = loads(wkt_string)
            geometry = shape(wkt_object)

            transformer = Transformer.from_crs('epsg:4326', src.crs, always_xy=True)
            transformed = transform(transformer.transform, geometry)

            out_image, out_transform = mask(src, [transformed], crop=True, nodata=np.nan, all_touched=True, filled=True)
            out_meta = src.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform,
                "nodata": np.nan
            })

            with rasterio.open(output_path, "w", **out_meta) as dest:
                dest.write(out_image)
        
        with rasterio.open(output_path) as src:
            data = src.read(1)
            stats = get_stat_values(data)
            return stats
    

    def save_image(self, local_path, object_path):
        with open(local_path, "rb") as file_obj:
            self.s3_client.upload_fileobj(file_obj, self.AWS_BUCKET_NAME, object_path)


    def save_stat_value(self, result):
        # Insert to raster_variables
        raster_variable = text("""
            insert into raster_variables (product_name, meta, variable_name, file_path, max_value, min_value, mean_value, med_value)
            values(:product_name, :meta, :variable_name, :file_path, :max_value, :min_value, :mean_value, :med_value)
        """)
        db.session.execute(raster_variable, {
            "product_name": result['metadata']['PRODUCT_NAME_ID'],
            "meta": json.dumps(result['percentage']) if result['type'] == 'NDVI' else None,
            "variable_name": result['type'].lower(),
            "file_path": result['object_path'],
            "max_value": result['stat_value']['max'],
            "min_value": result['stat_value']['min'],
            "mean_value": result['stat_value']['mean'],
            "med_value": result['stat_value']['med']
        })
        db.session.commit()

        # Get raster_variable_id
        raster_variable_id = db.session.execute(text("""
            select raster_variable_id 
            from raster_variables 
            where product_name = :product_name 
            and variable_name = :variable_name
            order by raster_variable_id desc limit 1
        """), {
            "product_name": result['metadata']['PRODUCT_NAME_ID'],
            "variable_name": result['type'].lower()
        }).fetchone()
        if raster_variable_id:
            raster_variable_id = raster_variable_id[0]

        if result['stage'] == 'original':
            # Check if geometry already exists
            check_query = text("select 1 from geometries where geom = st_geomfromtext(:geom, 4326, 'axis-order=long-lat')")

            # Get geometry_id if exists, if not, insert to geometries and pilot_geometries and get geometry_id
            if db.session.execute(check_query, {"geom": result['metadata']['WKT_COORDS']}).fetchone():
                geometry_id = db.session.execute(text("""
                    select geometry_id 
                    from geometries 
                    where geom = st_geomfromtext(:geom, 4326, 'axis-order=long-lat')
                """), {"geom": result['metadata']['WKT_COORDS']}).fetchone()
                if geometry_id:
                    geometry_id = geometry_id[0]
            else:
                geometries = text("""
                    insert into geometries (geom)
                    values(st_geomfromtext(:geom, 4326, 'axis-order=long-lat'))
                """)
                db.session.execute(geometries, {"geom": result['metadata']['WKT_COORDS']})
                db.session.commit()

                geometry_id = db.session.execute(text("""
                    select geometry_id
                    from geometries
                    where geom = st_geomfromtext(:geom, 4326, 'axis-order=long-lat')
                """), {"geom": result['metadata']['WKT_COORDS']}).fetchone()
                if geometry_id:
                    geometry_id = geometry_id[0]

                pilot_geometries = text("""
                    insert into pilot_geometries (pilot_id, geometry_id)
                    values(:pilot_id, :geometry_id)
                """)
                db.session.execute(pilot_geometries, {
                    "pilot_id": self.aoi['pilot_id'],
                    "geometry_id": geometry_id
                })
                db.session.commit()

        elif result['stage'] == 'cropped':
            geometry_id = result['metadata']['geometry_id']

        # Insert to data_catalogs
        data_catalogs = text("""
            insert into data_catalogs (user_id, fk_id, table_name, geometry_id, acquired_date, status, created_at)
            values(1, :fk_id, 'raster_variables', :geometry_id, :acquired_date, :status, now())
        """)
        db.session.execute(data_catalogs, {
            "fk_id": raster_variable_id,
            "geometry_id": geometry_id,
            "acquired_date": result['metadata']['DATE_ACQUIRED'],
            "status": result['stage']
        })
        db.session.commit()
    

if __name__ == "__main__":
    local_rawfile_path = '/home/fajarshiddiqqq/Public/amikom/silvanus/temp/S2A_MSIL2A_20200601T095041_N0500_R079_T33TWG_20230430T125143.SAFE.zip'
    aoi = {
        "pilot_id": 2,
        "pilot_name": "Gargano",
        "geometries": [
            {
                "geometry_id": 146,
                "geom": "POLYGON((15.90271 41.79384,15.90271 41.876719,16.058578 41.876719,16.058578 41.79384,15.90271 41.79384))"
            }
        ]
    }

    sentinel = ProcessSentinel(local_rawfile_path, aoi)
    sentinel.process()