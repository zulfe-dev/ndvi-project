from flask import Flask, render_template, request, jsonify
from geopy.geocoders import Nominatim
import logging
import rasterio
import re
from rasterio.warp import transform as rio_transform
from rasterio.windows import Window
import numpy as np
import os
from datetime import datetime, timedelta
from typing import Optional

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

NDVI_DIR = "Data/NDVI"

NDVI_PATTERN = re.compile(
    r"(?P<region>.+)_NDVI_(?P<start>\d{4}-\d{2}-\d{2})_to_(?P<end>\d{4}-\d{2}-\d{2})\.tif"
)

# ---------------- FUNCTIONS ----------------
def get_data_file_path(date: str, data_type: str, district: str = None) -> Optional[str]:
    """
    Get the file path for the requested data type and date.
    """
    try:
        # Parse date to get year
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        year = date_obj.year
        
        if data_type == "rainfall":
            # Check both new and old paths
            new_path = f"Data/RF/{year}/Total precipitation_Mean_{date}.tif"
            old_path = f"tiff/precipitation_Mean_{date}.tif"
            
            if os.path.exists(new_path):
                return new_path
            elif os.path.exists(old_path):
                return old_path
                
        elif data_type == "temperature_max":
            return f"Data/Temp/Max/{year}/2m temperature_MAX_{date}.tif"
            
        elif data_type == "temperature_min":
            return f"Data/Temp/MIn/{year}/2m temperature_MIN_{date}.tif"
            
        elif data_type == "temperature_mean":
            return f"Data/Temp/Mean/{year}/2m temperature_Mean_{date}.tif"
            
        elif data_type == "ndvi":
            # NDVI files have different naming pattern: {District}_NDVI_{date_range}.tif
            # We need to find the file that matches the district and contains the date
            ndvi_dir = "Data/NDVI"
            if os.path.exists(ndvi_dir) and district:
                # Try different district name variations
                district_variations = [
                    district.replace(" ", "_"),
                    district.replace(" ", ""),
                    district,
                    district.title().replace(" ", "_"),
                    district.upper().replace(" ", "_"),
                    district.lower().replace(" ", "_")
                ]
                
                for file in os.listdir(ndvi_dir):
                    if file.endswith('.tif'):
                        # Check if any district variation matches the filename
                        for district_var in district_variations:
                            if file.lower().startswith(district_var.lower()):
                                # Check if the date falls within the range in filename
                                # For now, return the first matching file
                                return os.path.join(ndvi_dir, file)
                
                # If no exact match, try to find any NDVI file for the region
                for file in os.listdir(ndvi_dir):
                    if file.endswith('.tif') and 'NDVI' in file:
                        return os.path.join(ndvi_dir, file)
            
        return None
    except Exception as e:
        logger.error(f"Error getting file path: {e}")
        return None


def get_data_at_location(lat: float, lon: float, tif_path: str) -> Optional[float]:
    """
    Return data value (float) from GeoTIFF nearest to (lat, lon).
    Returns None when the coordinate is outside the raster or the pixel is nodata.
    """
    try:
        with rasterio.open(tif_path) as ds:
            # Transform lon/lat (EPSG:4326) into dataset CRS if needed
            if ds.crs is not None and ds.crs.to_string() != "EPSG:4326":
                xs, ys = rio_transform("EPSG:4326", ds.crs, [lon], [lat])
                x, y = xs[0], ys[0]
            else:
                x, y = lon, lat

            # Get row, col
            row, col = ds.index(x, y)

            # Read single pixel window
            try:
                arr = ds.read(1, window=Window(col, row, 1, 1))
            except Exception:
                return None

            if arr.size == 0:
                return None

            val = arr[0, 0]
            if ds.nodata is not None and val == ds.nodata:
                return None
            if np.isnan(val):
                return None

            return float(val)

    except (IndexError, rasterio.errors.RasterioIOError) as e:
        logger.error(f"Error reading data from {tif_path}: {e}")
        return None


# ---------------- ROUTES ----------------
@app.route('/api/states')
def get_states():
    """Get list of all Indian states and union territories"""
    states = [
        "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
        "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
        "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
        "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
        "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
        "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu",
        "Delhi", "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry"
    ]
    return jsonify({"states": sorted(states)})


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/weather/get-data', methods=['POST'])
def get_weather_data():
    try:
        data = request.get_json()
        state = data.get('state')
        district = data.get('district')
        date = data.get('date')
        
        logger.info(f"Received request for state: {state}, district: {district}, date: {date}")

        # Initialize geocoder
        geolocator = Nominatim(user_agent="weather_data_app")

        # Geocode location
        location_name = f"{district}, {state}, India"
        location = geolocator.geocode(location_name)

        if not location:
            return jsonify({"status": "error", "message": "Location not found"}), 404

        # Get all data types including NDVI
        data_types = ['rainfall', 'temperature_max', 'temperature_min', 'temperature_mean', 'ndvi']
        weather_data = {}
        
        for data_type in data_types:
            # Get the appropriate data file path
            if data_type == 'ndvi':
                tiff_path = get_data_file_path(date, data_type, district)
            else:
                tiff_path = get_data_file_path(date, data_type)
            
            if tiff_path and os.path.exists(tiff_path):
                # Read data from TIFF
                value = get_data_at_location(location.latitude, location.longitude, tiff_path)
                weather_data[data_type] = value
                logger.info(f"{data_type}: {value} from {tiff_path}")
            else:
                weather_data[data_type] = None
                logger.warning(f"No data file found for {data_type}")
        
        result_data = {
            "state": state,
            "district": district,
            "date": date,
            "location": {
                "address": location.address,
                "latitude": location.latitude,
                "longitude": location.longitude
            },
            "weather_data": weather_data
        }

        logger.info(f"Location: {location.address}")
        logger.info(f"Latitude: {location.latitude}, Longitude: {location.longitude}")
        logger.info(f"Weather data: {weather_data}")

        return jsonify({"status": "success", "data": result_data})
        
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/weather/get-data-range', methods=['POST'])
def get_weather_data_range():
    """Get weather data for a date range"""
    try:
        data = request.get_json()
        state = data.get('state')
        district = data.get('district')
        from_date = data.get('from_date')
        to_date = data.get('to_date')
        
        logger.info(f"Received date range request for state: {state}, district: {district}, from: {from_date}, to: {to_date}")

        # Validate date range
        from_date_obj = datetime.strptime(from_date, "%Y-%m-%d")
        to_date_obj = datetime.strptime(to_date, "%Y-%m-%d")
        
        # Check if dates are within allowed range (2023-2024)
        min_date = datetime(2023, 1, 1)
        max_date = datetime(2024, 12, 31)
        
        if from_date_obj < min_date or to_date_obj > max_date:
            return jsonify({"status": "error", "message": "Dates must be between 2023 and 2024"}), 400
        
        if from_date_obj > to_date_obj:
            return jsonify({"status": "error", "message": "From date must be before or equal to To date"}), 400

        # Initialize geocoder and get location ONCE before the loop
        geolocator = Nominatim(user_agent="weather_data_app")
        location_name = f"{district}, {state}, India"
        
        logger.info(f"Geocoding location: {location_name}")
        location = geolocator.geocode(location_name)

        if not location:
            return jsonify({"status": "error", "message": "Location not found"}), 404

        logger.info(f"Location found: {location.address} (Lat: {location.latitude}, Lon: {location.longitude})")

        # Generate date range and process all dates
        current_date = from_date_obj
        date_results = []
        
        while current_date <= to_date_obj:
            date_str = current_date.strftime("%Y-%m-%d")
            
            # Get all data types including NDVI for this date
            data_types = ['rainfall', 'temperature_max', 'temperature_min', 'temperature_mean', 'ndvi']
            weather_data = {}
            
            for data_type in data_types:
                # Get the appropriate data file path
                if data_type == 'ndvi':
                    tiff_path = get_data_file_path(date_str, data_type, district)
                else:
                    tiff_path = get_data_file_path(date_str, data_type)
                
                if tiff_path and os.path.exists(tiff_path):
                    # Read data from TIFF
                    value = get_data_at_location(location.latitude, location.longitude, tiff_path)
                    weather_data[data_type] = value
                    logger.info(f"{date_str} - {data_type}: {value} from {tiff_path}")
                else:
                    weather_data[data_type] = None
                    logger.warning(f"{date_str} - No data file found for {data_type}")
            
            date_results.append({
                "date": date_str,
                "weather_data": weather_data
            })
            
            # Move to next day using timedelta
            current_date += timedelta(days=1)
        
        result_data = {
            "state": state,
            "district": district,
            "from_date": from_date,
            "to_date": to_date,
            "location": {
                "address": location.address,
                "latitude": location.latitude,
                "longitude": location.longitude
            },
            "date_results": date_results
        }

        logger.info(f"Location: {location.address}")
        logger.info(f"Latitude: {location.latitude}, Longitude: {location.longitude}")
        logger.info(f"Processed {len(date_results)} dates")

        return jsonify({"status": "success", "data": result_data})
        
    except Exception as e:
        logger.error(f"Error processing date range request: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/ndvi-map')
def ndvi_map():
    """NDVI Regional Map page"""
    return render_template('ndvi_map.html')


# -------------------------
# Helpers
# -------------------------
def extract_mean_ndvi(tif_path):
    with rasterio.open(tif_path) as ds:
        band = ds.read(1)
        return float(np.nanmean(band))

def date_in_range(target, start, end):
    return start <= target <= end

# -------------------------
# API
# -------------------------
@app.route("/api/ndvi/regional-analysis", methods=["POST"])
def regional_ndvi():
    payload = request.get_json()
    selected_date = datetime.strptime(payload["date"], "%Y-%m-%d").date()

    results = []

    for filename in os.listdir(NDVI_DIR):
        match = NDVI_PATTERN.match(filename)
        if not match:
            continue

        start = datetime.strptime(match["start"], "%Y-%m-%d").date()
        end = datetime.strptime(match["end"], "%Y-%m-%d").date()

        if not date_in_range(selected_date, start, end):
            continue

        region_name = match["region"].replace("_", " ").title()
        tif_path = os.path.join(NDVI_DIR, filename)

        ndvi_value = extract_mean_ndvi(tif_path)

        results.append({
            "name": region_name,
            "ndvi_value": round(ndvi_value, 3)
        })

    return jsonify({
        "status": "success",
        "data": {
            "regions": results
        }
    })


@app.route("/api/ndvi/available-dates", methods=["GET"])
def available_ndvi_dates():
    ranges = []

    for filename in os.listdir(NDVI_DIR):
        match = NDVI_PATTERN.match(filename)
        if not match:
            continue

        ranges.append({
            "start": match["start"],
            "end": match["end"]
        })

    return jsonify({
        "status": "success",
        "ranges": ranges
    })


def get_major_districts_for_state(state):
    """Get major districts for a state (simplified list)"""
    major_districts = {
        "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik", "Aurangabad", "Solapur", "Kolhapur", "Sangli", "Satara", "Ahmednagar"],
        "Karnataka": ["Bangalore", "Mysore", "Hubli", "Mangalore", "Belgaum", "Gulbarga", "Davangere", "Bellary", "Bijapur", "Shimoga"],
        "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli", "Salem", "Tirunelveli", "Erode", "Vellore", "Thoothukudi", "Dindigul"],
        "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot", "Bhavnagar", "Jamnagar", "Junagadh", "Gandhinagar", "Anand", "Bharuch"],
        "Rajasthan": ["Jaipur", "Jodhpur", "Kota", "Bikaner", "Ajmer", "Udaipur", "Bhilwara", "Alwar", "Bharatpur", "Sikar"],
        "Uttar Pradesh": ["Lucknow", "Kanpur", "Ghaziabad", "Agra", "Meerut", "Varanasi", "Allahabad", "Bareilly", "Aligarh", "Moradabad"],
        "West Bengal": ["Kolkata", "Howrah", "Durgapur", "Asansol", "Siliguri", "Malda", "Bardhaman", "Kharagpur", "Haldia", "Krishnanagar"],
        "Madhya Pradesh": ["Bhopal", "Indore", "Gwalior", "Jabalpur", "Ujjain", "Sagar", "Dewas", "Satna", "Ratlam", "Rewa"],
        "Andhra Pradesh": ["Hyderabad", "Visakhapatnam", "Vijayawada", "Guntur", "Nellore", "Kurnool", "Rajahmundry", "Tirupati", "Kakinada", "Anantapur"],
        "Telangana": ["Hyderabad", "Warangal", "Nizamabad", "Khammam", "Karimnagar", "Ramagundam", "Mahbubnagar", "Nalgonda", "Adilabad", "Suryapet"],
        "Kerala": ["Thiruvananthapuram", "Kochi", "Kozhikode", "Thrissur", "Kollam", "Palakkad", "Alappuzha", "Malappuram", "Kannur", "Kasaragod"],
        "Punjab": ["Ludhiana", "Amritsar", "Jalandhar", "Patiala", "Bathinda", "Mohali", "Firozpur", "Hoshiarpur", "Batala", "Pathankot"],
        "Haryana": ["Gurgaon", "Faridabad", "Panipat", "Ambala", "Yamunanagar", "Rohtak", "Hisar", "Karnal", "Sonipat", "Panchkula"],
        "Bihar": ["Patna", "Gaya", "Bhagalpur", "Muzaffarpur", "Purnia", "Darbhanga", "Bihar Sharif", "Arrah", "Begusarai", "Katihar"],
        "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela", "Brahmapur", "Sambalpur", "Puri", "Balasore", "Bhadrak", "Baripada", "Jharsuguda"],
        "Assam": ["Guwahati", "Silchar", "Dibrugarh", "Jorhat", "Nagaon", "Tinsukia", "Tezpur", "Bongaigaon", "Karimganj", "Sivasagar"]
    }
    
    return major_districts.get(state, [state.split()[0]])  # Fallback to state name if not found


def get_districts_for_state(state):
    """Get districts for a state (extended list for district-level analysis)"""
    # This is a simplified approach. In a real application, you'd have a comprehensive database
    major_districts = get_major_districts_for_state(state)
    
    # Add some additional districts for more comprehensive coverage
    additional_districts = {
        "Maharashtra": ["Thane", "Raigad", "Ratnagiri", "Sindhudurg", "Dhule", "Jalgaon", "Buldhana", "Akola", "Washim", "Amravati"],
        "Karnataka": ["Tumkur", "Hassan", "Mandya", "Chitradurga", "Kolar", "Chikmagalur", "Kodagu", "Dakshina Kannada", "Udupi", "Uttara Kannada"],
        "Tamil Nadu": ["Kanchipuram", "Tiruvallur", "Cuddalore", "Villupuram", "Dharmapuri", "Krishnagiri", "Namakkal", "Karur", "Perambalur", "Ariyalur"],
        "Gujarat": ["Mehsana", "Patan", "Banaskantha", "Sabarkantha", "Kheda", "Panchmahals", "Dahod", "Valsad", "Navsari", "Tapi"],
        "Rajasthan": ["Tonk", "Bundi", "Jhalawar", "Banswara", "Dungarpur", "Chittorgarh", "Rajsamand", "Pali", "Sirohi", "Jalore"]
    }
    
    extended_list = major_districts + additional_districts.get(state, [])
    return list(set(extended_list))  # Remove duplicates


@app.route('/api/rainfall/get-stats', methods=['POST'])
def get_rainfall_stats():
    """Legacy endpoint for backward compatibility"""
    try:
        data = request.get_json()
        # Support both old and new formats
        locality = data.get('locality') or data.get('district')
        state = data.get('state', 'Maharashtra')  # Default to Maharashtra for backward compatibility
        date = data.get('date')
        
        logger.info(f"Received legacy request for locality: {locality}, date: {date}")

        # Initialize geocoder
        geolocator = Nominatim(user_agent="geo_lat_long_app")

        # Geocode location
        location_name = f"{locality}, {state}, India"
        location = geolocator.geocode(location_name)

        data_dict = {
            "locality": locality,
            "date": date,
            "location": {
                "address": location.address if location else None,
                "latitude": location.latitude if location else None,
                "longitude": location.longitude if location else None
            }
        }

        # Read rainfall from TIFF (legacy path first, then new path)
        tiff_path = f"tiff/precipitation_Mean_{date}.tif"
        if not os.path.exists(tiff_path):
            # Try new path
            year = datetime.strptime(date, "%Y-%m-%d").year
            tiff_path = f"Data/RF/{year}/Total precipitation_Mean_{date}.tif"
            
        if location and os.path.exists(tiff_path):
            rainfall = get_data_at_location(location.latitude, location.longitude, tiff_path)
            if rainfall is not None:
                data_dict["rainfall"] = round(rainfall, 10)
            logger.info(f"Location: {location.address}")
            logger.info(f"Latitude: {location.latitude}, Longitude: {location.longitude}")
        else:
            logger.error("Location not found or data file missing")

        return jsonify({"status": "success", "data_received": data_dict})
        
    except Exception as e:
        logger.error(f"Error processing legacy request: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=8000)