# main.py
from datetime import datetime, timedelta
import pytz
from tda import auth, client
import secretsTDA
from gamma_analysis import calculate_gamma_exposure
from plotter import RealTimeGammaPlotter
import schedule
import time

class GammaExposureScheduler:
    def __init__(self):
        self.current_gamma_exposure = {}
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
        
        if self.client:
            r = self.client.get_option_chain(symbol='$SPX.X', contract_type=self.client.Options.ContractType.ALL, 
                                            from_date=use_date, to_date=use_date, strike_count=40)
            if r.status_code == 200:
                data = r.json()
                # Use the previous exposure as input to calculate the change in gamma exposure
                self.current_gamma_exposure, self.change_in_gamma_per_strike = calculate_gamma_exposure(data, self.previous_gamma_exposure)
                
                # Call the methods to update each plot individually
                self.plotter.update_plot_gamma(self.current_gamma_exposure)
                self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike)
                
            else:
                print(f"Failed to fetch data: {r.status_code}")

    def update_plots(self):
        self.plotter.update_plot_gamma(self.current_gamma_exposure)
        self.plotter.update_plot_change_in_gamma(self.change_in_gamma_per_strike)

    def schedule_job(self):
        # Adjust this to the desired frequency
        schedule.every(0.5).minutes.do(self.fetch_and_update_gamma_exposure)

    def run(self):
        self.authenticate()
        self.schedule_job()  # Call the scheduling method here
        while True:
            schedule.run_pending()
            time.sleep(30)  # Or adjust the sleep time as necessary

# Usage
scheduler = GammaExposureScheduler()
scheduler.run()