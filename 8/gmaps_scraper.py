from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from bs4 import BeautifulSoup
from argparse import ArgumentParser
import pandas as pd
import time, json

global driver
firefox_options = webdriver.FirefoxOptions()
firefox_options.add_argument("--window-size=800,2500")
driver = webdriver.Firefox(options=firefox_options)

def collect_location(object_type, around_location, maxpage):
    main_url = "https://www.google.com/maps/search/"

    # Construct search URL
    if around_location:
        merge_keyword = f"{object_type} near {around_location}"
        keyword = merge_keyword.replace(" ", "+")
        filename = merge_keyword.replace(" ", "_")
    else:
        keyword = object_type.replace(" ", "+")
        filename = object_type.replace(" ", "_")

    url = main_url + keyword
    driver.get(url)

    # Wait for the page to load
    time.sleep(3)
    location_collection = []

    for page in range(maxpage):
        try:
            # Wait for the sidebar pane to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//*[@id='pane']"))
            )

            # Scroll to load more locations in the sidebar
            for _ in range(5):
                sidebar = driver.find_element(By.XPATH, "//*[@id='pane']")
                driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight", sidebar)
                time.sleep(1)

            # Parse the loaded page
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            
            # Find all location entries
            location_items = soup.find_all('a', attrs={"aria-label": True, "href": True})
            
            # Extract location details
            for item in location_items:
                name = item.get('aria-label')
                url = item.get('href')
                if name and url:
                    if "google.com/maps/place/" in url:
                        location_collection.append({"name": name, "url": url})
                        print({"name": name, "url": url})

            print(f"Total locations collected so far: {len(location_collection)}")

            # Check if a next button exists
            try:
                next_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[@aria-label='Next page']"))
                )
                next_button.click()
                time.sleep(2)
            except TimeoutException:
                print("No next button found. Ending pagination.")
                break

        except Exception as e:
            print(f"Error on page {page + 1}: {e}")
            break

    return location_collection, filename

def get_location_data(url_loc):
    driver.get(url_loc)
    time.sleep(10)
    
    sidebar_pane = driver.find_element(by=By.XPATH, value="//*[@id='pane']/following-sibling::div/div/div")
    try:
        info_container = sidebar_pane.find_element(by=By.XPATH, value="//div/div[2]/div//div[contains(@aria-label, 'Information')]")
    except:
        info_container = sidebar_pane.find_element(by=By.XPATH, value="//div/div[2]/div//div[contains(@aria-label, 'Informasi')]")
            
    soup = BeautifulSoup(info_container.get_attribute('innerHTML'), 'html.parser')

    url_loc = driver.current_url
    try:
        name = url_loc.replace("https://www.google.com/maps/place/", "").split('/')
        name = name[0].replace('+',' ')
    except Exception as e:
        print(e)
        name = ""
    
    try:
        address = soup.select('button[aria-label*=Address]') or soup.select('button[aria-label*=Alamat]')
        address = address[0].text
        address = address.strip()
    except Exception as e:
        print(e)
        address = ""
    
    try:
        phone = soup.select('button[aria-label*=Phone]') or soup.select('button[aria-label*=Telepon]')
        phone = phone[0].text
        phone = phone.strip()
    except Exception as e:
        print(e)
        phone = ""
    
    try:
        website = soup.select('button[aria-label*=Website]') or soup.select("button[aria-label*='Situs Web']")
        website = website[0].text
        website = website.strip()
    except Exception as e:
        print(e)
        website = ""
    
    try:
        coordinate = url_loc.split('!')
        lat = coordinate[-2].split('d')[-1]
        lon = coordinate[-3].split('d')[-1]
    except Exception as e:
        print(e)
        lat = ""
        lon = ""

    
    store = {
        "name": name,
        "url": url_loc,
        "location": {
            "address": address,
            "coordinate": { "lat": lat, "lon": lon}
        },
        "phone": phone,
        "website": website
    }
    
    return store

def convert_to_json(data,filename):
    import geopandas as gpd
    from pandas import json_normalize

    df = json_normalize(data)
    gdf = gpd.GeoDataFrame(df, 
            geometry=gpd.points_from_xy(df['location.coordinate.lat'], df['location.coordinate.lon'], 
            crs="EPSG:4326"))
    gdf.to_file(f"{filename}.geojson", driver="GeoJSON")


if __name__ == "__main__":
    parser = ArgumentParser(description="Google Maps Destination Info Crawler")
    parser.add_argument('-o', dest='object', type=str, required=True)
    parser.add_argument('-al', dest='around_location', type=str)
    parser.add_argument('-l', dest='limit', type=int, default=5)
    args = parser.parse_args()

    res_cont = []
    object_type = args.object
    around_location = args.around_location
    maxpage = args.limit

    try:
        get_near_location_url, filename = collect_location(object_type, around_location, maxpage)
        
        for location in get_near_location_url:
            print("=" * 70)
            print(location['url'])
            result_location_data = get_location_data(location['url'])
            print(result_location_data)
            res_cont.append(result_location_data)
        
        convert_to_json(res_cont,filename)

    except Exception as e:
        print(e)
        driver.close()

    time.sleep(5)
    driver.close()