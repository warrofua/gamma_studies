import datetime

def calculate_gamma_exposure(data, previous_gamma_exposure=None):
    per_strike_gamma_exposure = {}
    change_in_gamma_per_strike = {}
    time_of_change_per_strike = {}
    total_gamma_exposure = 0
    contract_size = 100
    spot_price = data.get('underlyingPrice', 0)
    calculation_time = datetime.datetime.now()

    def add_gamma_exposure(option_type, strike, gamma, volume):
        gamma_exposure = spot_price * gamma * volume * contract_size * spot_price * 0.01
        strike = float(strike)

        if option_type == 'call':
            if strike > spot_price:  # OTM
                gamma_exposure = abs(gamma_exposure)  # Positive gamma as delta increases when approaching the strike
            elif strike < spot_price:  # ITM
                gamma_exposure = -abs(gamma_exposure)  # Negative gamma as adjustments become significant
                
        elif option_type == 'put':
            if strike < spot_price:  # OTM
                gamma_exposure = -abs(gamma_exposure)  # Negative gamma as delta increases when approaching the strike
            elif strike > spot_price:  # ITM
                gamma_exposure = abs(gamma_exposure)  # Positive gamma as adjustments become significant
        
        if strike in per_strike_gamma_exposure:
            per_strike_gamma_exposure[strike] += gamma_exposure
        else:
            per_strike_gamma_exposure[strike] = gamma_exposure

        if strike in previous_gamma_exposure:
            change = gamma_exposure - previous_gamma_exposure.get(strike, 0)
            change_in_gamma_per_strike[strike] = change
            time_of_change_per_strike[strike] = calculation_time

    for expiration_date, strikes in data.get('callExpDateMap', {}).items():
        for strike, options in strikes.items():
            for option in options:
                add_gamma_exposure('call', strike, option['gamma'], option['totalVolume'])

    for expiration_date, strikes in data.get('putExpDateMap', {}).items():
        for strike, options in strikes.items():
            for option in options:
                add_gamma_exposure('put', strike, option['gamma'], option['totalVolume'])

    total_gamma_exposure = sum(per_strike_gamma_exposure.values())
    largest_changes_with_time = sorted(change_in_gamma_per_strike.items(), key=lambda item: abs(item[1]), reverse=True)[:5]
    largest_changes = [(strike, change, time_of_change_per_strike[strike].strftime("%Y-%m-%d %H:%M:%S")) for strike, change in largest_changes_with_time]

    return total_gamma_exposure, per_strike_gamma_exposure, change_in_gamma_per_strike, largest_changes, spot_price