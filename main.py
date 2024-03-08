from datetime import datetime, timedelta, time
import pytz
from tda import auth, client
import secretsTDA
from gamma_analysis import calculate_gamma_exposure
from plotter import RealTimeGammaPlotter
import schedule
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import json
import time as time_module

class GammaExposureScheduler:
    def __init__(self):
        self.current_gamma_exposure = {}
        self.previous_gamma_exposure = {}
        self.change_in_gamma_per_strike = {}
        self.client = None
        self.plotter = RealTimeGammaPlotter()

    def authenticate(self):
        try:
            self.client = auth.client_from_token_file(secretsTDA.token_path, secretsTDA.api_key)
        except FileNotFoundError:
            from selenium import webdriver
            with webdriver.Chrome() as driver:
                self.client = auth.client_from_login_flow(
                    driver, secretsTDA.api_key, secretsTDA.redirect_uri,
                    secretsTDA.token_path)
                
    def fetch_and_update_gamma_exposure(self):
        eastern = pytz.timezone('US/Eastern')
        now_eastern = datetime.now(eastern)
        use_date = now_eastern.date() + timedelta(days=(1 if now_eastern.hour >= 16 else 0))
        print(use_date)
        
        if self.client:
            r = self.client.get_option_chain(symbol='$SPX.X', contract_type=self.client.Options.ContractType.ALL, 
                                            from_date=use_date, to_date=use_date, strike_count=30)
            if r.status_code == 200:
                data = r.json()
                total_gamma_exposure, self.current_gamma_exposure, self.change_in_gamma_per_strike, largest_changes = calculate_gamma_exposure(data, self.previous_gamma_exposure)
                self.previous_gamma_exposure = self.current_gamma_exposure.copy()

                # Update plots or any other logic to utilize the new data
                self.plotter.update_plot_gamma(self.current_gamma_exposure)
                self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike, largest_changes)
                self.plotter.show_plots()  # Ensure this is added to actually display the plots
                plt.pause(21)  # Give time for the plots to render                                  
                
            else:
                print(f"Failed to fetch data: {r.status_code}")

    def schedule_job(self):
        # Adjust this to the desired frequency
        schedule.every(0.33).minutes.do(self.fetch_and_update_gamma_exposure)

    def run(self):
        self.authenticate()
        self.schedule_job()  # Call the scheduling method here

        while True:
            now = datetime.now(pytz.timezone('US/Eastern'))
            if not (now.time() >= time(7, 28) and now.time() <= time(9, 28)):
                # It's outside the pause window, run pending tasks
                schedule.run_pending()
            else:
                print(f"Paused. Current time: {now.strftime('%Y-%m-%d %H:%M:%S')} EST")
            time_module.sleep(60)  # Sleep for a minute before checking again

# Usage
scheduler = GammaExposureScheduler()
scheduler.run()