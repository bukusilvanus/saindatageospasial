# Library yang dibutuhkan
from airflow import DAG
from airflow.operators.python_operator import PythonOperator, BranchPythonOperator
from airflow.operators.email_operator import EmailOperator
from airflow.operators.dummy import DummyOperator
from airflow.utils.dates import days_ago
from datetime import timedelta
import requests
import pandas as pd
import sqlite3
import os
import json

# Default arguments untuk DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

# Definisi DAG
with DAG(
    dag_id='dag_weather_data',
    default_args=default_args,
    description='Pipeline pengambilan data cuaca',
    schedule_interval='0 7 * * *',
    start_date=days_ago(1),
    catchup=False,
) as dag:
    # Fungsi untuk mengambil data cuaca
    def fetch_weather_data():
        api_key = '*******************'  # Ganti dengan API key OpenWeatherMap Anda
        city = 'Jakarta'
        url = f'http://api.openweathermap.org/data/2.5/weather?q={city}&appid={api_key}&units=metric'

        response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            os.makedirs('/opt/airflow/dags/data', exist_ok=True)
            with open('/opt/airflow/dags/data/weather_data.json', 'w') as f:
                f.write(response.text)
            return 'success'
        else:
            return 'failure'

    # Fungsi untuk menentukan cabang
    def branch_decision(ti):
        result = ti.xcom_pull(task_ids='fetch_weather_data_task')
        if result == 'success':
            return 'save_to_database_task'
        else:
            return 'send_failure_notification_task'

    # Fungsi untuk menyimpan data ke database
    def save_to_database():
        with open('/opt/airflow/dags/data/weather_data.json', 'r') as f:
            data = json.load(f)

        # Extract relevant data
        processed_data = {
            'city': data['name'],
            'temperature': data['main']['temp'],
            'weather': data['weather'][0]['description'],
            'timestamp': pd.Timestamp.now()
        }
        df = pd.DataFrame([processed_data])

        
        # simpan data ke SQLite database
        conn = sqlite3.connect('/opt/airflow/dags/data/weather_history.db')
        df.to_sql('weather_data', conn, if_exists='append', index=False)
        conn.close()

    # Fungsi untuk membuat laporan mingguan
    def generate_weekly_report():
        conn = sqlite3.connect('/opt/airflow/dags/data/weather_history.db')
        query = """
        SELECT 
            city, AVG(temperature) AS avg_temp, COUNT(*) AS data_points
        FROM 
            weather_data
        WHERE 
            timestamp >= date('now', '-7 days')
        GROUP BY city
        """
        df = pd.read_sql(query, conn)
        conn.close()

        # Save report to Excel
        report_path = '/opt/airflow/dags/data/weekly_weather_report.csv'
        df.to_csv(report_path, index=False)

    # Fungsi untuk mengirim notifikasi jika gagal
    def send_failure_notification():
        print("Failed to fetch weather data. Please check the API or network connection.")

    def send_success_notification():
        print("Success to fetch weather data.")

    # Task: Mengambil data cuaca
    fetch_weather_data_task = PythonOperator(
        task_id='fetch_weather_data_task',
        python_callable=fetch_weather_data,
    )

    # Task: Memutuskan cabang berdasarkan hasil pengambilan data
    branch_decision_task = BranchPythonOperator(
        task_id='branch_decision_task',
        python_callable=branch_decision,
    )

    # Task: Menyimpan data ke database
    save_to_database_task = PythonOperator(
        task_id='save_to_database_task',
        python_callable=save_to_database,
    )

    # Task: Membuat laporan mingguan
    generate_weekly_report_task = PythonOperator(
        task_id='generate_weekly_report_task',
        python_callable=generate_weekly_report,
    )

    # Task: Mengirim notifikasi jika gagal
    send_failure_notification_task = PythonOperator(
        task_id='send_failure_notification_task',
        python_callable=send_failure_notification,
    )

    # Task: Mengirim notifikasi jika berhasil
    send_success_notification_task = PythonOperator(
        task_id='send_success_notification_task',
        python_callable=send_success_notification,
    )

    # Dummy task untuk akhir pipeline
    end_task = DummyOperator(
        task_id='end_task',
        trigger_rule='one_success'
    )

    # Definisi alur eksekusi
    fetch_weather_data_task >> branch_decision_task
    branch_decision_task >> save_to_database_task >> generate_weekly_report_task >> send_success_notification_task >> end_task
    branch_decision_task >> send_failure_notification_task >> end_task
