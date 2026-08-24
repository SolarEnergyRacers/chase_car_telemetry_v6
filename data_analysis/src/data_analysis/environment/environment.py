

import numpy as np

from .sun_angles import *


def solar_power_gen(
    sun_power, 
    sun_visibility, 
    sun_angle, 
    panel_sqm, 
    panel_eff, 
    panel_angle, 
):
    sun_percentage = (  # negligible?
        atmospheric_travel_distance(sun_angle) / 
        atmospheric_travel_distance(0))
    sun_power = sun_power * sun_visibility**sun_percentage
    panel_cosine = np.cos((sun_angle-panel_angle)/180*np.pi)
    return sun_power * panel_cosine * panel_sqm * panel_eff


def forward_windspeed(
    wind_speed,
    wind_direction,
    car_direction
):
    angle = (car_direction - wind_direction)
    return np.cos(angle/180*np.pi)*wind_speed
