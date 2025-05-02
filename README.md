# Market Depth Visualization

This project visualizes level 2 options data from ThinkorSwim RTD using a bookmap style visualization.

```
TradingView lightweight charts.
Vite + React + Tailwind for the frontend.
FastAPI and SQLite for the backend.
```

This is a rough v1. Improvements coming.

![image](https://github.com/user-attachments/assets/534b719b-ce8a-41a7-97f3-ea5860596ec1)

## Historical View
![new_hist](https://github.com/user-attachments/assets/5fa77fef-932d-400a-9df5-90eeaa88bf94)


## To create a new chart
```
1. Enter a stock symbol
2. Select date
3. Select Call or Put
4. Enter a strike 
5. Click "Create Chart"
```

![new1](https://github.com/user-attachments/assets/8c58e9bd-fbac-473a-acf2-36a745624ccc)

## Add more charts
```
Click the "+" to add more charts. Click the red "x" to delete the chart.

Add as many charts as you want.
```

## Auto vs Mode1
```
Click "Auto" in the bottom right corner to switch to Manual mode.

The default "Auto" mode locks the y-axis to +-10% of the last price.
```

## Features

- Real-time visualization of order book data
- Historical view of price levels over time
- Heatmap showing order density
- Futures and Index options coming soon....

## Requirements

- Python 3.11+
- Node.js 14+

## Setup

1. Clone this repository
2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Install frontend dependencies:
   ```
   cd frontend
   npm install
   ```

## Running the Application

1. Start the API and data stream:
   ```
   python api.py
   ```

3. Start the frontend development server:
   ```
   cd frontend
   
   npm run dev
   ```

4. Open your browser and navigate to:
   ```
   http://localhost:8081
   ```

## API Endpoints

- `localhost:8080/symbols` - Get all available symbols in your db
- `localhost:8080/depth/{symbol}` - Get the latest market depth for a symbol
- `localhost:8080/historical_full/{symbol}` - Get historical market depth snapshots 

## Credit

[@2187Nick](https://x.com/2187Nick) 

[Discord](https://discord.com/invite/vxKepZ6XNC) Come checkout the other builders and projects.

Backend:

[PYRTDC](https://github.com/tifoji/pyrtdc/)

## Notes

1. This does not capture all trades or all market liquidity.
2. This is experimental and mainly just for exploring what's possible.