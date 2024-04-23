from datetime import datetime, timedelta
import pytz
from tda import auth, client
import secretsTDA
from gamma_analysis import calculate_gamma_exposure
from plotter import RealTimeGammaPlotter
import matplotlib.pyplot as plt
from selenium import webdriver
import time as time_module
from db_storage import store_raw_options_data

class GammaExposureScheduler:
    #initialize and create dictionaries for temporary data storage and analysis
    def __init__(self):
        self.current_gamma_exposure = {}
        self.previous_gamma_exposure = {}
        self.change_in_gamma_per_strike = {}
        self.client = None
        self.plotter = RealTimeGammaPlotter()

    #API auth
    def authenticate(self):
        try:
            self.client = auth.client_from_token_file(secretsTDA.token_path, secretsTDA.api_key)
        except FileNotFoundError:
            with webdriver.Chrome() as driver:
                self.client = auth.client_from_login_flow(driver, secretsTDA.api_key, secretsTDA.redirect_uri, secretsTDA.token_path)

    def fetch_and_update_gamma_exposure(self):
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)

        # If it's Friday after 4 PM, set use_date to the next Monday
        if now.weekday() == 4 and now.hour >= 16:
            use_date = (now + timedelta(days=3)).date()
        # Otherwise, use the next day's date if it's after 4 PM, or today's date if it's before 4 PM
        else:
            use_date = now.date() + timedelta(days=1 if now.hour >= 16 else 0)

        try:
            if self.client:
                r = self.client.get_option_chain(symbol='$SPX.X', contract_type=self.client.Options.ContractType.ALL, from_date=use_date, to_date=use_date, strike_count=50)
                if r.status_code == 200:
                    data = r.json()
                    total_gamma_exposure, self.current_gamma_exposure, self.change_in_gamma_per_strike, largest_changes, spot_price = calculate_gamma_exposure(data, self.previous_gamma_exposure)
                    self.previous_gamma_exposure = self.current_gamma_exposure.copy()
                    current_timestamp = datetime.now(pytz.timezone('US/Eastern'))
                    self.plotter.update_plot_gamma(self.current_gamma_exposure)
                    self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike, largest_changes)
                    self.plotter.update_total_gamma_exposure_plot(current_timestamp, total_gamma_exposure, spot_price)
                    self.plotter.show_plots()
                    pause_duration = 5
                else:
                    print(f"Failed to fetch data: {r.status_code}")
                    pause_duration = 5  # Longer pause when fetch fails
        except Exception as e:
            print(f"An error occurred: {e}")
            pause_duration = 5  # Longer pause on error
        finally:
            # Always attempt to store data in the database, even if the fetch or plotting fails
            db_params = {
                "dbname": "spx_options_data",
                "user": "postgres",
                "password": "password",
                "host": "localhost"
            }
            # Make sure to handle the case where data might not be defined due to failed fetch
            if 'data' in locals():
                store_raw_options_data(db_params, data, now)
            else:
                print("No data to store in database.")

            plt.pause(pause_duration)  # Adjust pause based on operation outcome

    def run(self):
        self.authenticate()
        
        while True:
            eastern = pytz.timezone('US/Eastern')
            now = datetime.now(eastern)
            start_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            end_time = now.replace(hour=16, minute=15, second=0, microsecond=0)

            # Check if current time is within the trading hours
            if start_time <= now <= end_time:
                self.fetch_and_update_gamma_exposure()
            else:
                print("Outside trading hours. Waiting to resume...")

            time_module.sleep(4) #sleep until time run API request again

scheduler = GammaExposureScheduler()
scheduler.run()
