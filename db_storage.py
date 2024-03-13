import psycopg2
import json

def store_raw_options_data(db_params, data, now):
    try:
        # Connect to the PostgreSQL database
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor()
        
        # Insert the data into the spx_options_data table
        cur.execute("INSERT INTO spx_options_data (data, fetched_at) VALUES (%s, %s)", (json.dumps(data), now))
        
        # Commit the transaction
        conn.commit()
        
        # Close the cursor and connection
        cur.close()
        conn.close()
        print("Data stored successfully.")
    except Exception as e:
        print(f"Failed to store raw options data: {e}")