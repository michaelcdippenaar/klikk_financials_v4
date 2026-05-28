"""Curated global market events used as chart and vector-search context."""

MAJOR_MARKET_EVENTS = [
    {
        'slug': 'who-covid-pandemic',
        'date': '2020-03-11',
        'title': 'WHO characterises COVID-19 as a pandemic',
        'publisher': 'WHO',
        'link': 'https://www.who.int/europe/emergencies/situations/covid-19',
        'summary': (
            'The World Health Organization characterised COVID-19 as a pandemic. '
            'This is a broad global risk marker for liquidity, consumer demand, '
            'supply chains, interest rates and market volatility.'
        ),
        'scope': 'global macro',
    },
    {
        'slug': 'south-africa-lockdown',
        'date': '2020-03-23',
        'title': 'South Africa nationwide lockdown announced',
        'publisher': 'SA Government',
        'link': 'https://www.sanews.gov.za/south-africa/president-ramaphosa-announces-nationwide-lockdown',
        'summary': (
            'South Africa announced a nationwide lockdown. This marker is relevant '
            'to JSE shares because it affected trading conditions, consumer activity, '
            'logistics, employment and local earnings expectations.'
        ),
        'scope': 'South Africa macro',
    },
    {
        'slug': 'trump-reciprocal-tariffs',
        'date': '2025-04-02',
        'title': 'Trump reciprocal tariffs announced',
        'publisher': 'White House',
        'link': 'https://www.whitehouse.gov/presidential-actions/2025/04/regulating-imports-with-a-reciprocal-tariff-to-rectify-trade-practices-that-contribute-to-large-and-persistent-annual-united-states-goods-trade-deficits/',
        'summary': (
            'The United States announced reciprocal tariff measures. This is a '
            'global trade marker for import costs, exporters, retailers, commodity '
            'demand, currency moves and broad risk appetite.'
        ),
        'scope': 'global trade',
    },
]
