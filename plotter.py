import matplotlib.pyplot as plt

class RealTimeGammaPlotter:
    def __init__(self):
        # Set up for gamma exposure plot
        self.fig_gamma, self.ax_gamma = plt.subplots(figsize=(14, 8))
        # Set up for change in gamma plot
        self.fig_change_in_gamma, self.ax_change_in_gamma = plt.subplots(figsize=(14, 8))

    def init_plot_gamma(self):
        self.ax_gamma.clear()
        self.ax_gamma.set_title('Per Strike Gamma Exposure')
        self.ax_gamma.set_xlabel('Strike Price')
        self.ax_gamma.set_ylabel('Gamma Exposure')

    def init_plot_change_in_gamma(self):
        self.ax_change_in_gamma.clear()
        self.ax_change_in_gamma.set_title('Change in Gamma Exposure Per Strike')
        self.ax_change_in_gamma.set_xlabel('Strike Price')
        self.ax_change_in_gamma.set_ylabel('Change in Gamma')

    def update_plot_gamma(self, current_gamma_exposure):
        self.init_plot_gamma()

        strikes = list(current_gamma_exposure.keys())
        gamma_exposures = [current_gamma_exposure[strike] for strike in strikes]
        strikes_str = [str(strike) for strike in strikes]

        self.ax_gamma.bar(strikes_str, gamma_exposures, color='skyblue', label='Current Gamma Exposure')
        self.ax_gamma.legend()
        plt.draw()

    def update_plot_change_in_gamma(self, change_in_gamma_per_strike):
        self.init_plot_change_in_gamma()

        strikes = list(change_in_gamma_per_strike.keys())
        changes_in_gamma = [change_in_gamma_per_strike[strike] for strike in strikes]
        strikes_str = [str(strike) for strike in strikes]

        self.ax_change_in_gamma.bar(strikes_str, changes_in_gamma, color='red', label='Change in Gamma')
        self.ax_change_in_gamma.legend()
        plt.draw()

    def show_plots(self):
        plt.show(block=False)
