import psycopg2
import json

def store_raw_options_data(db_params, data, timestamp):
    """Store raw options data in the database.
    
    Parameters:
    - db_params: A dictionary with database connection parameters.
    - data: The raw JSON data to store.
    - timestamp: The datetime when the data was fetched.
    """
    connection = None
    try:
        connection = psycopg2.connect(**db_params)
        cursor = connection.cursor()
        insert_query = """
        INSERT INTO options_data_raw (data, fetched_at)
        VALUES (%s, %s);
        """
        cursor.execute(insert_query, (json.dumps(data), timestamp))
        connection.commit()
    except Exception as e:
        print(f"Failed to store raw options data: {e}")
    finally:
        if connection is not None:
            connection.close()