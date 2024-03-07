def calculate_gamma_exposure(data, previous_gamma_exposure=None):
    per_strike_gamma_exposure = {}  # Dictionary to store gamma exposure per strike
    change_in_gamma_per_strike = {}  # Dictionary to store the change in gamma exposure per strike
    total_gamma_exposure = 0
    contract_size = 100  # Standard contract size for US options
    spot_price = data.get('underlyingPrice', 0)  # Safely get spot price
    
    print("Spot price:", spot_price)

    if 'callExpDateMap' in data and 'putExpDateMap' in data and spot_price != 0:
        def add_gamma_exposure(strike, gamma_exposure):
            if strike in per_strike_gamma_exposure:
                per_strike_gamma_exposure[strike] += gamma_exposure
            else:
                per_strike_gamma_exposure[strike] = gamma_exposure

            # Calculate the delta in gamma exposure if previous data is available
            if previous_gamma_exposure and strike in previous_gamma_exposure:
                change_in_gamma_per_strike[strike] = gamma_exposure - previous_gamma_exposure[strike]
            else:
                change_in_gamma_per_strike[strike] = gamma_exposure

        print("Processing calls...")
        for expiration_date in data['callExpDateMap']:
            for strike, options in data['callExpDateMap'][expiration_date].items():
                for option in options:
                    gamma = option['gamma']
                    volume = option['totalVolume']
                    call_exposure = spot_price * gamma * volume * contract_size * spot_price * 0.01
                    add_gamma_exposure(strike, call_exposure)

        print("Processing puts...")
        for expiration_date in data['putExpDateMap']:
            for strike, options in data['putExpDateMap'][expiration_date].items():
                for option in options:
                    gamma = option['gamma']
                    volume = option['totalVolume']
                    put_exposure = -spot_price * gamma * volume * contract_size * spot_price * 0.01
                    add_gamma_exposure(strike, put_exposure)

        total_gamma_exposure = sum(per_strike_gamma_exposure.values())
        print("Total gamma exposure calculated successfully.")
        
    else:
        print("Data missing expected keys or invalid spot price. Check data format.")

    print(f"Final Total Gamma Exposure: {total_gamma_exposure}")
    for strike, exposure in per_strike_gamma_exposure.items():
        print(f"Strike {strike}: {exposure}")

    return total_gamma_exposure, per_strike_gamma_exposure, change_in_gamma_per_strike
