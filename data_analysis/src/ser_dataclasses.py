

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Car_coeffs:
    """Constant parameters of car used in simulating its movement"""
    weight: float = 200       # kg
    panel_sqm: float = 4.0    # m² solar cell area
    panel_eff: float = 0.2    # solar cell efficiency (sqm + eff actually one param?)
    battery_cap: float = 5e3  # Wh battery capacity
    maxpow: float = 3000      # motor power limit
    v1_coeff: float = 0.0     # linear    speed (v¹) coefficient
    v2_coeff: float = 0.0     # squared   speed (v²) coefficient
    v3_coeff: float = 0.0     # cubic     speed (v³) coefficient
    v4_coeff: float = 0.0     # 4th order speed (v⁴) coefficient
    vu_coeff: float = 0.0     # vertical upward   speed coefficient
    vd_coeff: float = 0.0     # vertical downward speed coefficient

# dynamic stuff?
# panel_angle (-> charging stops)
# xz_direction (compass direction/azimuth -> wind speed)
# battery_voltage


@dataclass
class Environment:
    """Current state of the environment"""
    # at_time: datetime             # time at which this applies [todo]
    sun_power: float      = 1000. # W/m² solar power
    sun_visibility: float = 0.99  # atmospheric effects
    wind_speed: float     = 0.0   # m/s
    wind_direction: float = 0.0   # °, 0° = North

    def to_tuple(self):  # demo workaround for interpolation
        return (self.sun_power, self.sun_visibility, self.wind_speed, self.wind_direction)
    @classmethod
    def from_tuple(cls, data: tuple):
        obj = cls()
        obj.sun_power, obj.sun_visibility, obj.wind_speed, obj.wind_direction = data
        return obj
