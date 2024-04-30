import matplotlib.pyplot as plt
import matplotlib as mpl
import matplotlib.dates as mdates
from datetime import datetime
from collections import deque
import numpy as np

# Adjust global font size
mpl.rcParams.update({'font.size': mpl.rcParams['font.size'] - 4})
class RealTimeGammaPlotter:
    def __init__(self):
        plt.ion()  # Turn on interactive mode
        self.fig, self.ax = plt.subplots(3, 1, figsize=(14, 24))  # Create 3 subplots: gamma, change in gamma, and total gamma exposure over time
        self.ax2 = self.ax[2].twinx()  # Create a twin y-axis for the spot price on total gamma exposure over time chart
        
        # Initialize data structures to store time and total gamma exposure
        self.total_gamma_exposure_times = [] #times 
        self.total_gamma_exposures = []
        self.spot_prices = []
        self.largest_changes_strikes = []
        self.largest_changes_values = []

        # Deques to track strikes of largest positive and negative changes for the last 'x' updates
        deque_length = 100 # mean and std. deviation length /x/
        self.positive_changes_strikes = deque(maxlen=deque_length)
        self.negative_changes_strikes = deque(maxlen=deque_length)

        # For historical mean and std tracking
        self.mean_positive_history = []
        self.std_positive_history = []
        self.mean_negative_history = []
        self.std_negative_history = []

    def init_plots(self):
        self.ax[0].clear()
        self.ax[0].set_title('Per Strike Gamma Exposure')
        self.ax[0].set_xlabel('')
        self.ax[0].set_ylabel('Gamma Exposure')

        self.ax[1].clear()
        self.ax[1].set_title('')
        self.ax[1].set_xlabel('')
        self.ax[1].set_ylabel('Change in Gamma Exposure')

        self.ax[2].clear()
        self.ax[2].set_title('')
        self.ax[2].set_xlabel('Time')
        self.ax[2].set_ylabel('Total Gamma Exposure', color='blue')
        self.ax2.clear()
        self.ax2.set_ylabel('SPX Spot Price ($)', color='green')

    def update_plot_gamma(self, current_gamma_exposure):
        self.init_plots()

        strikes = list(current_gamma_exposure.keys())
        gamma_exposures = [current_gamma_exposure[strike] for strike in strikes]
        strikes_str = [str(strike) for strike in strikes]

        self.ax[0].bar(strikes_str, gamma_exposures, color='skyblue', label='Current Gamma Exposure ($Bn)')
        self.ax[0].tick_params(axis='x', rotation=90)  # Rotate x-axis labels for better readability
        self.ax[0].legend()

    def update_plot_change_in_gamma(self, change_in_gamma_per_strike, largest_changes):
        strikes = list(change_in_gamma_per_strike.keys())
        changes_in_gamma = [change_in_gamma_per_strike[strike] for strike in strikes]
        strikes_str = [str(strike) for strike in strikes]

        self.ax[1].bar(strikes_str, changes_in_gamma, color='lightgrey', label='Change in Gamma ($Bn)')

        # Annotate largest changes
        for top_strike, top_change, _ in largest_changes:  # Assume largest_changes includes the time as a third element now
            if str(top_strike) in strikes_str:
                index = strikes_str.index(str(top_strike))
                self.ax[1].bar(strikes_str[index], top_change, color='red')
                self.ax[1].text(strikes_str[index], top_change, f'{top_change:.2f}', ha='center')

                # Store the largest changes for plotting on the total exposure graph
                self.largest_changes_strikes.append(top_strike)
                self.largest_changes_values.append(top_change)

                if top_change > 0:
                    self.positive_changes_strikes.append(top_strike)
                elif top_change < 0:
                    self.negative_changes_strikes.append(top_strike)

        self.ax[1].tick_params(axis='x', rotation=90)
        self.ax[1].legend()

    def update_total_gamma_exposure_plot(self, time_stamp, total_gamma_exposure, spot_price):
        self.total_gamma_exposure_times.append(time_stamp)
        self.total_gamma_exposures.append(total_gamma_exposure)
        self.spot_prices.append(spot_price)

        # plot the total gamma exposure over time on the left axis, plot the spot prices on the right axis
        self.ax[2].plot(self.total_gamma_exposure_times, self.total_gamma_exposures, 'b-', label='Total Gamma Exposure')
        self.ax2.plot(self.total_gamma_exposure_times, self.spot_prices, 'g-', label='SPX Spot Price ($)')

        # On the right axis, plot dots at strikes for the largest changes 
        for time, change, strike in zip(self.total_gamma_exposure_times, self.largest_changes_values, self.largest_changes_strikes):
            color = 'red' if change < 0 else 'green'
            self.ax2.scatter([time], strike, color=color, s=10, marker='o', zorder=5)

        self.ax[2].legend(loc='upper left')
        self.ax[2].tick_params(axis='y', labelcolor='blue')

        self.ax2.legend(loc='upper right')
        self.ax2.tick_params(axis='y', labelcolor='green')

        # Update and plot historical means and standard deviations
        if self.positive_changes_strikes:
            mean_positive = np.mean(list(self.positive_changes_strikes))
            std_dev_positive = np.std(list(self.positive_changes_strikes))
            self.mean_positive_history.append(mean_positive)
            self.std_positive_history.append(std_dev_positive)

            self.ax2.plot(self.total_gamma_exposure_times[-len(self.mean_positive_history):], self.mean_positive_history, 'lightgreen', label='Mean Strike Positive Changes')
            lower_bound = np.array(self.mean_positive_history) - np.array(self.std_positive_history)
            upper_bound = np.array(self.mean_positive_history) + np.array(self.std_positive_history)
            self.ax2.fill_between(self.total_gamma_exposure_times[-len(self.mean_positive_history):], lower_bound, upper_bound, color='lightgreen', alpha=0.3, label='Std Dev Positive Changes')

        if self.negative_changes_strikes:
            mean_negative = np.mean(list(self.negative_changes_strikes))
            std_dev_negative = np.std(list(self.negative_changes_strikes))
            self.mean_negative_history.append(mean_negative)
            self.std_negative_history.append(std_dev_negative)

            self.ax2.plot(self.total_gamma_exposure_times[-len(self.mean_negative_history):], self.mean_negative_history, 'lightcoral', label='Mean Strike Negative Changes')
            lower_bound = np.array(self.mean_negative_history) - np.array(self.std_negative_history)
            upper_bound = np.array(self.mean_negative_history) + np.array(self.std_negative_history)
            self.ax2.fill_between(self.total_gamma_exposure_times[-len(self.mean_negative_history):], lower_bound, upper_bound, color='lightcoral', alpha=0.3, label='Std Dev Negative Changes')

        # Format the x-axis to display dates nicely
        self.ax[2].xaxis.set_major_locator(mdates.AutoDateLocator())
        self.ax[2].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))

        plt.draw()

    def show_plots(self):
        plt.show(block=False)
