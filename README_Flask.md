# 🌦️ Weather Data Dashboard - Flask Edition

A comprehensive web application built with Flask to visualize rainfall, temperature, and NDVI (vegetation index) data from GeoTIFF files for all states of India. Features interactive bar charts for data visualization over date ranges.

## Features

- **Interactive Web Interface**: Clean, responsive UI with gradient backgrounds
- **All Indian States**: Dropdown with all 28 states and 8 union territories
- **Date Range Selection**: Select any date range between 2023-2024 (no limit on range length)
- **Smart Display Mode**:
  - **Single Day**: Interactive map with location marker showing all weather data in popup
  - **Multiple Days**: Bar charts for rainfall, temperature, and NDVI data visualization
- **Interactive Map (Leaflet.js)**: For single-day requests, displays location on map with weather data popup
- **Interactive Charts (Chart.js)**: For multi-day requests, displays bar charts for data trends
- **Comprehensive Environmental Data**: Displays rainfall, temperature (max, min, mean), and NDVI simultaneously
- **NDVI Support**: Normalized Difference Vegetation Index for vegetation health monitoring
- **Smart Data Routing**: Automatically finds data in both new and legacy folder structures
- **Location Intelligence**: Geocoding with Nominatim for any district in India
- **Collapsible Details**: Toggle detailed daily data view
- **Real-time Results**: Live data fetching with loading indicators
- **API Endpoints**: RESTful APIs for integration with other applications

## Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Application
```bash
python app.py
```

### 3. Access the Dashboard
Open your browser and go to: `http://127.0.0.1:8000`

## How to Use

1. **Select State**: Choose from dropdown of all Indian states
2. **Enter District**: Type the district name
3. **Pick Date Range**: Select "From Date" and "To Date" (2023-2024 only, no range limit)
4. **Get Data**: Click "Get Weather Data"
5. **View Results**:
   - **Single Day**: See an interactive map with a marker showing all weather data in a popup
   - **Multiple Days**: View three interactive bar charts showing trends over time
6. **Toggle Details**: Click "Show Detailed Daily Data" to see day-by-day breakdown (for multi-day requests)

## Data Visualization

### Single Day View (Map)
- **Interactive Map**: Powered by Leaflet.js with OpenStreetMap tiles
- **Location Marker**: Pinpoints the exact location on the map
- **Weather Popup**: Click marker to see all weather data:
  - 🌧️ Rainfall (mm)
  - 🌡️ Temperature Max, Min, Mean (°C)
  - 🌱 NDVI (Vegetation Index)
- **Zoom Controls**: Zoom in/out to explore the area

### Multi-Day View (Charts)
- **Rainfall Chart**: Bar chart showing daily precipitation in mm
- **Temperature Chart**: Multi-series bar chart with Max, Mean, and Min temperatures
- **NDVI Chart**: Bar chart displaying vegetation index values
- **Features**: Hover tooltips, responsive design, color-coded data series

## Data Structure

The application supports the following data sources:

### Rainfall Data
- **New Path**: `Data/RF/{year}/Total precipitation_Mean_{date}.tif`
- **Legacy Path**: `tiff/precipitation_Mean_{date}.tif`

### Temperature Data
- **Max**: `Data/Temp/Max/{year}/2m temperature_MAX_{date}.tif`
- **Min**: `Data/Temp/MIn/{year}/2m temperature_MIN_{date}.tif`
- **Mean**: `Data/Temp/Mean/{year}/2m temperature_Mean_{date}.tif`

### NDVI Data
- **Path**: `Data/NDVI/{District}_NDVI_{date_range}.tif`
- **Note**: NDVI files use district-specific naming and date ranges

## API Endpoints

### Get Indian States
```
GET /api/states

Response:
{
  "states": ["Andhra Pradesh", "Assam", "Bihar", ...]
}
```

### Single Date Environmental Data API
```
POST /api/weather/get-data
Content-Type: application/json

{
  "state": "Maharashtra",
  "district": "Mumbai",
  "date": "2023-01-01"
}
```

### Date Range Environmental Data API (NEW)
```
POST /api/weather/get-data-range
Content-Type: application/json

{
  "state": "Maharashtra",
  "district": "Mumbai",
  "from_date": "2023-01-01",
  "to_date": "2023-01-07"
}
```

### Legacy Rainfall API (Backward Compatible)
```
POST /api/rainfall/get-stats
Content-Type: application/json

{
  "locality": "Mumbai",
  "date": "2023-01-01"
}
```

## Example Response (Date Range)

```json
{
  "status": "success",
  "data": {
    "state": "Maharashtra",
    "district": "Mumbai",
    "from_date": "2023-01-01",
    "to_date": "2023-01-07",
    "location": {
      "address": "Mumbai, Mumbai Suburban, Maharashtra, 400051, India",
      "latitude": 19.054999,
      "longitude": 72.8692035
    },
    "date_results": [
      {
        "date": "2023-01-01",
        "weather_data": {
          "rainfall": 0.0003902253811247647,
          "temperature_max": 26.779958724975586,
          "temperature_min": 19.974750518798828,
          "temperature_mean": 23.212820053100586,
          "ndvi": 0.08605938404798508
        }
      },
      ...
    ]
  }
}
```

## Data Types Explained

### Weather Data
- **Rainfall**: Precipitation in millimeters (mm)
- **Temperature Max**: Maximum temperature in Celsius (°C)
- **Temperature Min**: Minimum temperature in Celsius (°C)
- **Temperature Mean**: Average temperature in Celsius (°C)

### Environmental Data
- **NDVI**: Normalized Difference Vegetation Index (0.0 to 1.0)
  - Values closer to 1.0 indicate healthier, denser vegetation
  - Values closer to 0.0 indicate sparse or unhealthy vegetation
  - Negative values typically indicate water bodies or bare soil

## Supported Locations

The application works with any district in any Indian state or union territory:

### States (28)
Andhra Pradesh, Arunachal Pradesh, Assam, Bihar, Chhattisgarh, Goa, Gujarat, Haryana, Himachal Pradesh, Jharkhand, Karnataka, Kerala, Madhya Pradesh, Maharashtra, Manipur, Meghalaya, Mizoram, Nagaland, Odisha, Punjab, Rajasthan, Sikkim, Tamil Nadu, Telangana, Tripura, Uttar Pradesh, Uttarakhand, West Bengal

### Union Territories (8)
Andaman and Nicobar Islands, Chandigarh, Dadra and Nagar Haveli and Daman and Diu, Delhi, Jammu and Kashmir, Ladakh, Lakshadweep, Puducherry

## File Structure

```
├── app.py                 # Main Flask application
├── templates/
│   └── index.html        # Web interface template (with Chart.js)
├── static/
│   └── style.css         # Additional CSS styles
├── Data/                 # Environmental data directory
│   ├── RF/              # Rainfall data
│   ├── Temp/            # Temperature data
│   └── NDVI/            # Vegetation index data
├── requirements.txt      # Python dependencies
└── README_Flask.md      # This file
```

## Key Features in Latest Version

1. **Smart Display Mode**: Automatically switches between map view (1 day) and chart view (multiple days)
2. **Interactive Map with Leaflet.js**: Single-day requests show location on map with weather data popup
3. **Date Range Selection**: No limit on date range length (previously 30 days)
4. **Interactive Bar Charts**: Three separate charts for rainfall, temperature, and NDVI
5. **Chart.js Integration**: Professional data visualization with hover tooltips
6. **Collapsible Details**: Toggle button to show/hide detailed daily data
7. **Multi-Series Temperature Chart**: Shows Max, Mean, and Min temperatures together
8. **Responsive Charts & Map**: Adapts to screen size and maintains aspect ratio
9. **Color-Coded Data**: Each data type has distinct colors for easy identification
10. **Optimized Geocoding**: Single geocoding call per request for better performance

## Chart Features

- **Rainfall Chart**: Blue bars showing precipitation levels
- **Temperature Chart**: Orange (Max), Yellow (Mean), Blue (Min) bars
- **NDVI Chart**: Green bars showing vegetation index
- **Interactive Tooltips**: Hover over bars to see exact values
- **Axis Labels**: Clear labeling with units (mm, °C, index)
- **Date Rotation**: Angled date labels for better readability

## Development

The Flask application runs in debug mode by default. For production deployment:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

## Technologies Used

- **Flask**: Web framework
- **Rasterio**: GeoTIFF file processing
- **Geopy**: Geocoding services
- **NumPy**: Numerical computations
- **Chart.js**: Interactive data visualization (charts)
- **Leaflet.js**: Interactive map visualization
- **OpenStreetMap**: Map tiles and geographic data
- **HTML/CSS/JavaScript**: Frontend interface

## License

This project is open source and available under the MIT License.