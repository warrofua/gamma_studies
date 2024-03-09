from datetime import datetime, timedelta
import pytz
from tda import auth, client
import secretsTDA
from gamma_analysis import calculate_gamma_exposure
from plotter import RealTimeGammaPlotter
import matplotlib.pyplot as plt
from selenium import webdriver
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
            with webdriver.Chrome() as driver:
                self.client = auth.client_from_login_flow(driver, secretsTDA.api_key, secretsTDA.redirect_uri, secretsTDA.token_path)

    def fetch_and_update_gamma_exposure(self):
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern)

        if now.weekday() == 4 and now.hour >= 16:
            # If it's Friday after 4 PM, set use_date to the next Monday
            days_until_monday = (7 - now.weekday()) % 7 + 3
            use_date = (now + timedelta(days=days_until_monday)).date()
        else:
            # Otherwise, use the next day's date if it's after 4 PM, or today's date if it's before 4 PM
            use_date = now.date() + timedelta(days=1 if now.hour >= 16 else 0)

        if self.client:
            r = self.client.get_option_chain(symbol='$SPX.X', contract_type=self.client.Options.ContractType.ALL, from_date=use_date, to_date=use_date, strike_count=30)
            if r.status_code == 200:
                data = r.json()
                total_gamma_exposure, self.current_gamma_exposure, self.change_in_gamma_per_strike, largest_changes = calculate_gamma_exposure(data, self.previous_gamma_exposure)
                self.previous_gamma_exposure = self.current_gamma_exposure.copy()
                self.plotter.update_plot_gamma(self.current_gamma_exposure)
                self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike, largest_changes)
                self.plotter.show_plots() 
                plt.pause(14)
            else:
                print(f"Failed to fetch data: {r.status_code}")

    def run(self):
        self.authenticate()
        
        while True:
            self.fetch_and_update_gamma_exposure()
            time_module.sleep(15)

scheduler = GammaExposureScheduler()
scheduler.run()