

# taken from https://github.com/JPL-Evapotranspiration-Algorithms/sun-angles
# (Apache 2.0 license)
# reason being weird dependency chain with rasters->ECOv002-calval-tables

# also useful:
# https://www.suncalc.org/
# https://basicfreetools.com/sun-angle-shadow-calculator/

from   datetime import datetime
import numpy as np


# <not part of JPL code>
def atmospheric_travel_distance(SZA_deg: Union[float, np.ndarray]):
    """
    Calculate the distance the sun travels through the atmosphere at given zenith angle.
    Assume uniform atmosphere of 50km height (~ Tropos- + Stratosphere).

    Parameters:
    SZA_deg (Union[float, np.ndarray]): Solar zenith angle in degrees.
    
    Returns:
    Union[float, np.ndarray]: atmosphere travel in meter.
    """
    re =     6371e3  # meter [earth radius]
    ra = re +  80e3
    angle = np.radians(90-SZA_deg)  # SZA: 0° = top down; angle: 90° = top down
    alpha = np.pi/2 + angle
    beta  = np.arcsin(re * np.sin(alpha) / ra)  # law of sines
    gamma = np.pi - alpha - beta
    return (re**2 + ra**2 - 2*re*ra*np.cos(gamma))**0.5  # law of cosines
# </not part of JPL code>


def calculate_SZA_from_datetime(time_UTC: datetime, lat: float, lon: float):
    """
    Calculates the solar zenith angle (SZA) in degrees based on the given UTC time, latitude, and longitude.

    Args:
        time_UTC (datetime.datetime): The UTC time to calculate the SZA for.
        lat (float): The latitude in degrees.
        lon (float): The longitude in degrees.

    Returns:
        float: The calculated solar zenith angle in degrees.
    """
    # Calculate the day of year based on the UTC time and longitude
    doy = solar_day_of_year_for_longitude(time_UTC, lon)
    # Calculate the hour of the day based on the UTC time and longitude
    hour = solar_hour_of_day_for_longitude(time_UTC, lon)
    # Calculate the solar zenith angle in degrees based on the latitude, solar declination angle, and hour of the day
    # SZA = calculate_SZA_from_DOY_and_hour(lat, lon, doy, hour)
    SZA = calculate_SZA_from_DOY_and_hour(lat, doy, hour)

    # Return the calculated solar zenith angle
    return SZA


def solar_day_of_year_for_longitude(
    time_UTC: datetime, 
    lon: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """
    Calculate the solar day of year for a given UTC time and longitude(s).

    Parameters
    ----------
    time_UTC : datetime
        The UTC time to calculate the day of year for.
    lon : float or np.ndarray
        Longitude(s) in degrees.

    Returns
    -------
    float or np.ndarray
        The calculated day of year.
    """
    # Support single datetime or list/array/Series of datetimes
    import numpy as np
    import pandas as pd

    def process_single_time(single_time, lon):
        DOY_UTC = single_time.timetuple().tm_yday
        hour_UTC = single_time.hour + single_time.minute / 60 + single_time.second / 3600
        offset = UTC_offset_hours_for_longitude(lon)
        hour_of_day = hour_UTC + offset
        DOY = DOY_UTC
        # Adjust the day of year if the hour of day is outside the range [0, 24]
        if hour_of_day < 0:
            DOY -= 1
        if hour_of_day > 24:
            DOY += 1
        return DOY

    # Handle list, np.ndarray, pd.Series
    if isinstance(time_UTC, (list, np.ndarray, pd.Series)):
        # If lon is array-like, broadcast
        if isinstance(lon, (list, np.ndarray, pd.Series)):
            return np.array([process_single_time(t, l) for t, l in zip(time_UTC, lon)])
        else:
            return np.array([process_single_time(t, lon) for t in time_UTC])
    else:
        return process_single_time(time_UTC, lon)

def solar_hour_of_day_for_longitude(
    time_UTC: datetime, 
    lon: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """
    Calculate the solar hour of day for a given UTC time and longitude.

    Parameters
    ----------
    time_UTC : datetime
        The UTC time.
    geometry : rt.RasterGeometry
        The raster geometry object with longitude information.

    Returns
    -------
    rt.Raster
        The hour of the day as a raster.
    """
    hour_UTC = time_UTC.hour + time_UTC.minute / 60 + time_UTC.second / 3600
    UTC_offset_hours = UTC_offset_hours_for_longitude(lon)
    hour_of_day = hour_UTC + UTC_offset_hours
    hour_of_day = np.where(hour_of_day < 0, hour_of_day + 24, hour_of_day)
    hour_of_day = np.where(hour_of_day > 24, hour_of_day - 24, hour_of_day)

    return hour_of_day #if len(hour_of_day) > 1 else hour_of_day[0]

def UTC_offset_hours_for_longitude(
    lon: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """
    Calculate the UTC offset in hours for a given raster geometry.

    Parameters
    ----------
    geometry : rt.RasterGeometry
        The raster geometry object with longitude information.

    Returns
    -------
    rt.Raster
        The UTC offset in hours as a raster.
    """
    return np.radians(lon) / np.pi * 12


def calculate_SZA_from_DOY_and_hour(
        lat: Union[float, np.ndarray], 
        # lon: Union[float, np.ndarray], 
        DOY: Union[float, np.ndarray], 
        hour: Union[float, np.ndarray]) -> Union[float, np.ndarray]:
    """
    Calculates the solar zenith angle (SZA) in degrees based on the given UTC time, latitude, longitude, day of year, and hour of day.

    Args:
        lat (Union[float, np.ndarray]): The latitude in degrees.
        lon (Union[float, np.ndarray]): The longitude in degrees.
        doy (Union[float, np.ndarray, Raster]): The day of year.
        hour (Union[float, np.ndarray, Raster]): The hour of the day.

    Returns:
        Union[float, np.ndarray, Raster]: The calculated solar zenith angle in degrees.
    """
    day_angle_rad = day_angle_rad_from_DOY(DOY)
    solar_dec_deg = solar_dec_deg_from_day_angle_rad(day_angle_rad)
    SZA = SZA_deg_from_lat_dec_hour(lat, solar_dec_deg, hour)

    return SZA

def day_angle_rad_from_DOY(DOY: np.ndarray) -> np.ndarray:
    """
    Calculate the day angle in radians from the day of the year.

    Parameters:
    DOY (Union[Raster, np.ndarray]): A Raster object or a numpy array containing 
    day of the year values (integers between 1 and 365).

    Returns:
    Union[Raster, np.ndarray]: A Raster object or a numpy array containing the 
    corresponding day angles in radians.

    The day angle is calculated using the formula:
    day_angle = (2 * π * (DOY - 1)) / 365
    
    This formula converts the day of the year into an angle in radians, 
    with 0 radians representing the start of the year (DOY=1) and 
    2π radians representing the end of the year (DOY=365).

    Reference:
    Duffie, J. A., & Beckman, W. A. (2013). Solar Engineering of Thermal Processes (4th ed.). Wiley.
    """
    # Accept lists, scalars, or arrays; convert to np.ndarray if needed
    if isinstance(DOY, list):
        DOY = np.array(DOY)
    return (2 * np.pi * (DOY - 1)) / 365

def solar_dec_deg_from_day_angle_rad(day_angle_rad: np.ndarray) -> np.ndarray:
    """
    Calculate solar declination in degrees from the day angle in radians.

    Parameters:
    day_angle_rad (Union[Raster, np.ndarray]): A Raster or numpy array containing day angles in radians.

    Returns:
    Union[Raster, np.ndarray]: A Raster or numpy array containing the corresponding solar declination angles in degrees.

    The solar declination is calculated using the following formula:
    solar_declination = 0.006918 - 0.399912 * cos(day_angle_rad) + 0.070257 * sin(day_angle_rad)
                      - 0.006758 * cos(2 * day_angle_rad) + 0.000907 * sin(2 * day_angle_rad)
                      - 0.002697 * cos(3 * day_angle_rad) + 0.00148 * sin(3 * day_angle_rad)
    
    This formula converts the day angle in radians to the solar declination in degrees, 
    which represents the angle between the rays of the sun and the plane of the Earth's equator.

    Reference:
    Duffie, J. A., & Beckman, W. A. (2013). Solar Engineering of Thermal Processes (4th ed.). Wiley.
    """
    return (0.006918 - 0.399912 * np.cos(day_angle_rad) + 0.070257 * np.sin(day_angle_rad) 
            - 0.006758 * np.cos(2 * day_angle_rad) + 0.000907 * np.sin(2 * day_angle_rad) 
            - 0.002697 * np.cos(3 * day_angle_rad) + 0.00148 * np.sin(3 * day_angle_rad)) * (180 / np.pi)

def SZA_deg_from_lat_dec_hour(
        latitude: np.ndarray, 
        solar_dec_deg: np.ndarray, 
        hour: np.ndarray) -> np.ndarray:
    """
    This function calculates the solar zenith angle (SZA) given the latitude, solar declination, and solar time. 
    The SZA is the angle between the zenith and the center of the sun's disc. The zenith is the point on the celestial 
    sphere directly above a specific location on the earth's surface.

    The calculation is based on the formula:
    cos(SZA) = sin(latitude) * sin(solar declination) + cos(latitude) * cos(solar declination) * cos(hour angle)

    The function has been validated against the MOD07 product, with the SZA calculated by this function matching 
    the SZA provided by MOD07 to within 0.4 degrees.

    Parameters:
    :param latitude: Latitude of the location in degrees. Ranges from -90 (South Pole) to 90 (North Pole).
    :param solar_dec_deg: Solar declination in degrees. It is the tilt of the Earth's axis relative to the sun and varies throughout the year.
    :param hour: Solar time in hours. It is the time based on the position of the sun in the sky, and varies throughout the day from 0 to 24.

    Returns:
    :return: Solar zenith angle in degrees. Ranges from 0 (sun directly overhead) to 90 (sun on the horizon).

    References:
    Muneer, T., & Fairooz, F. (2005). Solar radiation model. Applied energy, 81(4), 419-437.
    """
    # Convert latitude from degrees to radians for computation
    latitude_rad = np.radians(latitude)

    # Convert solar declination from degrees to radians for computation
    solar_dec_rad = np.radians(solar_dec_deg)

    # Calculate the hour angle in degrees. The hour angle is the angular distance between the sun and the meridian plane.
    # It is positive before noon and negative after noon. The formula used here converts solar time to hour angle.
    hour_angle_deg = hour * 15.0 - 180.0

    # Convert the hour angle from degrees to radians for computation
    hour_angle_rad = np.radians(hour_angle_deg)

    # Calculate the solar zenith angle in radians using the formula:
    SZA_rad = np.arccos(np.sin(latitude_rad) * np.sin(solar_dec_rad) + np.cos(latitude_rad) * np.cos(solar_dec_rad) * np.cos(hour_angle_rad))

    # Convert the solar zenith angle from radians to degrees for the final output
    SZA_deg = np.degrees(SZA_rad)

    # Return the solar zenith angle in degrees
    return SZA_deg


def calculate_solar_azimuth(
        solar_dec_deg: np.ndarray, 
        SZA_deg: np.ndarray, 
        hour: np.ndarray) -> np.ndarray:
    """
    Calculate the solar azimuth angle based on the solar declination, solar zenith angle, and hour of the day.
    
    Parameters:
    solar_dec_deg (Union[Raster, np.ndarray]): Solar declination in degrees.
    SZA_deg (Union[Raster, np.ndarray]): Solar zenith angle in degrees.
    hour (Union[Raster, np.ndarray]): Hour of the day, where 0 corresponds to 00:00 and 23 corresponds to 23:00.
    
    Returns:
    Union[Raster, np.ndarray]: Solar azimuth angle in degrees.
    
    Note:
    This function ignores any warnings that might be generated during the calculations.
    
    References:
    Muneer, T., & Fairooz, F. (2005). Solar radiation and daylight models: for the energy efficient design of buildings. Architectural Press.
    """
    with warnings.catch_warnings():
        # Ignore warnings that might be generated during the calculations
        warnings.filterwarnings('ignore')
        
        # Convert the solar declination from degrees to radians
        solar_dec_rad = np.radians(solar_dec_deg)
        # Convert the solar zenith angle from degrees to radians
        SZA_rad = np.radians(SZA_deg)
        # Calculate the hour angle in degrees and convert it to radians
        hour_angle_deg = hour * 15.0 - 180.0
        # convert hour angle to radians
        hour_angle_rad = np.radians(hour_angle_deg)
        # Calculate the solar azimuth in radians using the formula provided in the docstring
        solar_azimuth_rad = np.arcsin(-1.0 * np.sin(hour_angle_rad) * np.cos(solar_dec_rad) / np.sin(SZA_rad))
        # Convert the solar azimuth from radians to degrees
        solar_azimuth_deg = np.degrees(solar_azimuth_rad)
    
    return solar_azimuth_deg
