import matplotlib.pyplot as plt
import matplotlib as mpl
import matplotlib.dates as mdates
from datetime import datetime

# Adjust global font size
mpl.rcParams.update({'font.size': mpl.rcParams['font.size'] - 4})
class RealTimeGammaPlotter:
    def __init__(self):
        plt.ion()  # Turn on interactive mode
        self.fig, self.ax = plt.subplots(3, 1, figsize=(14, 24))  # Create 3 subplots: gamma, change in gamma, and total gamma exposure over time
        self.ax2 = self.ax[2].twinx()  # Create a twin y-axis for the spot price on total gamma exposure over time chart
        
        # Initialize data structures to store time and total gamma exposure
        self.total_gamma_exposure_times = []
        self.total_gamma_exposures = []
        self.spot_prices = []

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

        self.ax[1].tick_params(axis='x', rotation=90)
        self.ax[1].legend()

    def update_total_gamma_exposure_plot(self, time_stamp, total_gamma_exposure, spot_price):
        self.total_gamma_exposure_times.append(time_stamp)
        self.total_gamma_exposures.append(total_gamma_exposure)
        self.spot_prices.append(spot_price)

        # Plot total gamma exposure on the primary y-axis of the third plot
        self.ax[2].plot(self.total_gamma_exposure_times, self.total_gamma_exposures, 'b-', label='Total Gamma Exposure ($Bn)')
        self.ax[2].legend(loc='upper left')
        self.ax[2].tick_params(axis='y', labelcolor='blue')

        # Plot spot price on the secondary y-axis
        self.ax2.plot(self.total_gamma_exposure_times, self.spot_prices, 'g-', label='SPX Spot Price')
        self.ax2.legend(loc='upper right')
        self.ax2.tick_params(axis='y', labelcolor='green')

        # Format the x-axis to display dates nicely
        self.ax[2].xaxis.set_major_locator(mdates.AutoDateLocator())
        self.ax[2].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))

        plt.draw()

    def show_plots(self):
        plt.show(block=False)
