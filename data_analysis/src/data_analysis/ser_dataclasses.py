

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Car_coeffs:
    """Constant parameters of car used in simulating its movement.

    Anchor for all drive coefficients: 14 Wh/km at 72.5 km/h, battery side,
    aux included.  14 Wh/km = 14 * 3600 / 1000 = 50.400 J/m
      aux share:            18 W / 20.139 m/s  =  0.894 J/m
      drive input:                             = 49.506 J/m
    Reference speed 72.5 km/h / 3.6            = 20.139 m/s
    Drivetrain efficiency eta = 0.90 (controller + motor), constant by choice.
    """

    weight: float = 230        # kg, incl. driver and ballast. ESTIMATE, not
                               # calculated. Scales v1/vu/vd linearly ->
                               # replace with scrutineering scale value.
    panel_sqm: float = 6.0     # m² solar cell area: 387 cells * 155 cm² = 5.999
    panel_eff: float = 0.21    # 0.254 (Maxeon Gen7 mid bin)
                               #  * 0.97 optics * 0.95 mismatch/curvature
                               #  * 0.99 wiring * 0.99 MPPT (Elmar 98.1-99.4 %)
                               #  * 0.98 soiling
                               #  * 0.924 cell temp (-0.27 %/K * 28 K) = 0.2077
                               # bin range: 0.204 (24.9 %) .. 0.212 (25.9 %)
                               # -> peak 6.0 * 1000 * 0.208 = 1248 W
                               # Applied to measured/forecast irradiance
                               # (GHI driving, tracking GTI parked), so it must
                               # NOT contain any sun-angle or cloud term - the
                               # irradiance already does. The 28 K temperature
                               # rise assumes near-full sun; under cloud the
                               # cells run cooler and the true value is a few
                               # percent higher. Second order, left flat.
                               # NOTE: only the product panel_sqm * panel_eff
                               # enters the physics; a fit can never separate
                               # them.  Kept apart for readability: panel_sqm is
                               # fixed by the rules, panel_eff is the calibrated
                               # one (MPPT currents vs. GHI, day 1).
    battery_cap: float = 2879  # Wh = 6P * 4.30 Ah (rated min @10 A) * 31S * 3.6 V
                               #    = 25.80 Ah * 111.6 V = 2879.3
                               # SUPERSEDED by Battery_coeffs below, which
                               # gets the same number from the OCV curve
                               # (2909 Wh) instead of V_nom * Ah.  Kept only
                               # so existing callers do not break; new code
                               # must use battery.capacity_wh(batt).  Two
                               # numbers for one pack is how they drift apart.
    battery_usable: float = 0.90   # fraction of battery_cap planned as usable.
                               # SUPERSEDED by Battery_coeffs.usable.
                               # 2879 * 0.90 = 2591 Wh.  Separate field on
                               # purpose: day 8 (finish 15:00) may go deeper
                               # than day 1.  BMS cut-off and the weakest cell
                               # set the real floor, not the 2.5 V datasheet value.
    maxpow: float = 5580       # W = 50 A fuse * 111.6 V nominal.
                               # Not limiting: climbing needs 9.1 % gradient
                               # headroom at 72.5 km/h, steepest stages are below.

    v1_coeff: float = 10.03    # W/(m/s), rolling: Crr * m * g / eta
                               #   = 0.004 * 230 * 9.81 / 0.90 = 10.028
                               # Crr = 0.004 is GUESSED, no published value for
                               # the Bridgestone RA01AZ.  Only free number left.
    v2_coeff: float = 0.0      # no physical counterpart (power ~ v²)
    v3_coeff: float = 0.1415    # Anker 14 Wh/km bei 60 km/h:
                                 #   (14*3.6 - 18/16.667 - 10.028) / 16.667^2
                                 #   = 39.292 / 277.78
                                 # implizites CdA = 0.1415*0.90*2/1.02 = 0.250 m2
                                 # Der Anker ist ein ERINNERTER Wert, keine Messung.
                                 # 0.250 m2 ist fuer diese Klasse hoch (typisch
                                 # 0.10-0.15), also bewusst pessimistisch. Die
                                 # Unsicherheit ist eine Groessenordnung, kein Prozent.
    v4_coeff: float = 0.0      # no physical counterpart (power ~ airspeed⁴)
    vu_coeff: float = 2507     # J per metre climbed: m * g / eta
                               #   = 230 * 9.81 / 0.90 = 2507.0
    vd_coeff: float = 1692     # J per metre descended: m * g * eta_regen
                               #   = 230 * 9.81 * 0.75 = 1692.2
                               # POSITIVE, because v_speed is already negative
                               # in the code -> term becomes a negative power.
                               # 0.75 = 0.80 dyno (motor+inverter) minus pack
                               # I²R minus the share not recovered because the
                               # car is allowed to run faster downhill.

    rho_ref: float = 1.02      # kg/m³, air density the anchor was measured at.
                               # Origin unknown, assumed Highveld (conservative):
                               #   p(1350 m) = 101325 * (1 - 2.25577e-5 * 1350)^5.2559
                               #             = 86146 Pa
                               #   rho = 86146 / (287.05 * 293.15) = 1.024
                               # drive_power scales v3 by rho_local / rho_ref.
                               # After the day-1 fit: set to the mean density of
                               # the fitted run -> stops being an assumption.
    aux_power: float = 18.0    # W = 8 electronics + 10 daylights.  Constant over
                               # TIME, not distance -> belongs in the energy
                               # integral, not in v1 (would be infinite at standstill).
    wind_height_factor: float = 0.65   # dimensionless. Open-Meteo delivers
                               # wind at 10 m; the car's frontal area sits
                               # between 0 and ~1.2 m, where the wind is
                               # slower. Logarithmic wind profile with a
                               # roughness length z0 = 0.05 m (open veld):
                               #   u(1 m)/u(10 m) = ln(1/0.05) / ln(10/0.05)
                               #                  = 3.00 / 5.30 = 0.57
                               # taken as 0.65 for an effective height a bit
                               # above 1 m and smoother road surroundings.
                               # NOT a small correction: 8 m/s reported becomes
                               # 5.2 m/s at the car, and at 72.5 km/h headwind
                               # that is 20.2 instead of 24.4 Wh/km.
                               # A rough terrain guess; the day-1 fit cannot
                               # separate it from CdA, so treat it as fixed.
    wheel_circumference: float = 1.75   # m = pi * 0.557 m (outer dia., Attachment II)
                               # unloaded; loaded 2-3 % less.  Calibrate from
                               # mc_erpm vs. GPS speed on a straight run.

@dataclass
class Environment:
    """Environment at one point in space and time.

    Field names match the keys of environment.CANONICAL_VARS, which is the
    single place that maps them onto Open-Meteo variable names. Units are
    already converted there (pressure in Pa).

    The former clear-sky fields (sun_power, sun_visibility) are gone: GHI
    and GTI from Open-Meteo already contain the atmosphere, the cloud cover
    and the sun elevation, so there is nothing left to model here. Cloud
    cover was the largest single unknown of a race day and is now data
    rather than an assumption.

    For a whole journey, use RouteWeather.sample() and work on the returned
    DataFrame directly - it is vectorised. This dataclass is for single
    points, defaults and hand-built scenarios ("what if it is overcast all
    afternoon").
    """
    ghi: float            = 0.0   # W/m² global horizontal irradiance.
                                  # Correct for a flat-lying panel, which is
                                  # what the car has while driving. The slight
                                  # curvature of the array is neglected; the
                                  # 0.95 mismatch/curvature factor inside
                                  # panel_eff absorbs part of it. Do not also
                                  # apply a cosine term - GHI is horizontal.
    gti_tracking: float   = 0.0   # W/m² on a 2-axis sun-tracking plane, i.e.
                                  # a parked car with its panel aimed at the
                                  # sun (control stop, loop stop). Fetched with
                                  # tilt=azimuth=nan.
    dni: float            = 0.0   # W/m² direct normal. Unused so far; needed
                                  # only if plane-of-array for a tilted or
                                  # curved panel is ever modelled.
    dhi: float            = 0.0   # W/m² diffuse horizontal. Same.
    wind_speed: float     = 0.0   # m/s at 10 m (Open-Meteo reference height).
                                  # Scale by car.wind_height_factor before
                                  # using it on the car.
    wind_direction: float = 0.0   # °, 0° = North, METEOROLOGICAL convention:
                                  # the direction the wind comes FROM. Same
                                  # angle as the car azimuth therefore means
                                  # headwind (see total_Ws_for_lap()).
    temperature: float    = 20.0  # °C, 2 m air temperature. Used for air
                                  # density only; the daily swing of ~20 K is
                                  # an 8 % density effect on the v3 term.
    pressure_msl: float   = 101325.  # Pa, pressure reduced to sea level.
                                  # Route altitude does the heavy lifting for
                                  # density (18 % Sasolburg -> Paarl); the
                                  # synoptic variation here is only 1-2 %, but
                                  # it comes free in the same weather request.

    @classmethod
    def from_series(cls, s):
        """Build from one row of RouteWeather.sample() (or any mapping with
        the canonical keys). Unknown keys are ignored, absent ones keep
        their default."""
        obj = cls()
        for f in cls.__dataclass_fields__:
            if f in s:
                setattr(obj, f, float(s[f]))
        return obj

    def to_tuple(self):
        return tuple(getattr(self, f) for f in self.__dataclass_fields__)

    @classmethod
    def from_tuple(cls, data: tuple):
        obj = cls()
        for f, v in zip(cls.__dataclass_fields__, data):
            setattr(obj, f, v)
        return obj


@dataclass(frozen=True)
class Battery_coeffs:
    """Constant parameters of the battery pack.

    Frozen so it can key the cached energy curve in simulation/battery.py.
    Build a modified pack with dataclasses.replace(batt, usable=0.95).

    Everything here is per CELL except serial_cells / parallel_cells /
    pack_r_extra_ohm / usable.  Samsung INR21700-45T, datasheet in the
    project.
    """

    serial_cells:   int = 31    # -> 111.6 V nominal
    parallel_cells: int = 6

    cell_capacity_ah: float = 4.30  # Ah.  Datasheet 3.2 "rated discharge
                               # capacity", min 4300 mAh, measured at a 10 A
                               # discharge.  Datasheet 3.1 gives typ 4500 /
                               # min 4400 mAh at 0.2 C.  The car draws about
                               # 1000 W at 72.5 km/h = 9 A pack = 1.5 A per
                               # cell = 0.33 C, far closer to the 0.2 C test
                               # than to the 10 A one, so 4.30 is conservative
                               # by roughly 4 % (~120 Wh on the pack).
                               # ESTIMATE either way: cells that have cycled
                               # lose capacity, and the datasheet only
                               # guarantees 60 % after 250 cycles at 35 A.
                               # A capacity test is the only way to settle it.
    v_nominal_cell: float = 3.6   # V, datasheet 3.3.  Used only for the
                               # nominal-Wh comparison, never for energy.

    cell_r_i_ohm: float = 0.015   # ohm, DC internal resistance per cell.
                               # GUESSED: this datasheet does not state a DC
                               # or AC IR.  0.010-0.016 is the usual range for
                               # a 45T.  Enters the OCV correction linearly:
                               # at 25 A pack it is 1.55 V (R=0.012) to 2.58 V
                               # (R=0.020) of pack sag, and on the plateau
                               # 10 mV per cell is 1.3 % SoC - so this one
                               # number is worth several percent of SoC.
    pack_r_extra_ohm: float = 0.010  # ohm, everything the cells are not:
                               # busbars, fuse, contactors, shunt, wiring to
                               # the voltage sense point.  GUESSED placeholder.
                               # Measurable in five minutes from telemetry:
                               # V at rest minus V under a known load, divided
                               # by the current, minus cell_r_i * S / P.
                               # Do that before trusting any voltage-based SoC.

    # Open-circuit voltage over state of charge, one cell.  Taken over from
    # SER_strategy_sosol_2026/myFunctions.py unchanged, so results stay
    # comparable to the old notebooks.  It is a GENERIC li-ion curve, not the
    # 45T: end points match (2.5 V cut-off, 4.2 V full) but the plateau shape
    # is where the error sits, and the plateau is most of the race.  Replace
    # with the 0.2 C discharge curve digitised from the datasheet figure,
    # shifted by I*R_i to get OCV, or with a slow discharge of the real pack.
    ocv_soc: tuple = (0.0, 0.02, 0.10, 0.30, 0.50, 0.70, 0.90, 1.0)
    ocv_v:   tuple = (2.5, 3.0,  3.3,  3.5,  3.65, 3.8,  4.0,  4.2)

    usable: float = 0.90       # fraction of pack energy the strategy may
                               # plan with.  ESTIMATE.  The real floor is set
                               # by the BMS cut-off and the weakest cell, not
                               # by the 2.5 V datasheet value.  Day 8 (finish
                               # 15:00) may justify going deeper than day 1.

    temp_note: str = ("resistance and capacity are taken at room temperature. "
                      "Cell R_i roughly doubles near 0 C; a Highveld start at "
                      "5 C therefore sags more than this model says, and the "
                      "first hour of the day is the worst case.")

    @property
    def capacity_ah(self) -> float:
        return self.cell_capacity_ah * self.parallel_cells

    @property
    def v_nominal(self) -> float:
        return self.v_nominal_cell * self.serial_cells
