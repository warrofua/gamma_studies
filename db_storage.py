import psycopg2
import json

def store_raw_options_data(db_params, data, now):
    try:
        # Optionally, print a snapshot of the data to be stored for inspection
        print("Snapshot of data to be stored:")
        print(json.dumps(data, indent=4, sort_keys=True)[:3000])  # Prints the first 1000 characters of the JSON data
        
        # Connect to the PostgreSQL database
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor()
        
        # Insert the data into the spx_options_data table
        cur.execute("INSERT INTO spx_options_data (data, fetched_at) VALUES (%s, %s)", (json.dumps(data), now))
        
        # Commit the transaction
        conn.commit()
        
        # Indicate successful storage
        print("Data stored successfully.")
        
        # Close the cursor and connection
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to store raw options data: {e}")