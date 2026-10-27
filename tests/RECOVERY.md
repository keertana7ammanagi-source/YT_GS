# Recovery guidance (offline)

Everything here runs offline: no Wi-Fi, internet or API keys. The Recovery tab is read-only with respect to the CanSat
(it never sends commands).

## Landing point
After the firmware reports IMPACT (state 7), each frame is checked and, if valid, added to a window of fixes. A fix is
valid when `GNSS_FIX > 0`, latitude/longitude are finite, in range and not exactly 0,0, and `GNSS_HDOP` is positive and
not above 5.0. When 5 valid fixes are available and all lie within 10 m of their average, the average is locked as the
landing point. If the fixes keep moving, the window slides (so an early outlier drops out); after 30 valid fixes it locks
the latest average and says **LOW CONFIDENCE**. These numbers are tunable defaults (`LandingEstimator(...)` in
`offline_navigation.py`), not validated limits. **Reset landing lock** clears the lock; starting a new simulation also does.

## Directions
Enter your latitude, longitude and heading (degrees clockwise from north), or connect a GPS receiver, then press
**Get Directions**. You get a turn instruction, the compass direction (8 points, e.g. north-east), distance and bearing.

## Optional GPS receiver
Connect any receiver that streams NMEA over a serial port (USB GPS puck, GPS module on a USB-serial adapter). Choose its
own port and baud rate (commonly 9600) - it cannot be the LoRa telemetry port. GGA and RMC sentences with a valid checksum
are used. Latitude/longitude fill the entries automatically and directions refresh with each fix. The GPS *course* is
the direction you are travelling, so it replaces the heading only while you move (>= 1 knot, about 0.5 m/s); when you
stand still your manual heading is kept. The app only reads from the receiver; it never writes to it.

## Faster simulation
Switch on **Simulation Mode**, then **Fast IMPACT test** (Recovery tab), then **Start Telemetry**. IMPACT arrives after
12 packets instead of 60 and the landing point locks about 17 s after Start Telemetry. Simulated ground fixes carry about
1-2 m of noise so the averaging is exercised. Simulated data is not real telemetry (TEAM_ID `SIM-0001`).

## Limitations
* Averaging cannot correct a systematic GNSS error (e.g. a receiver that settled 20 m off); it only reduces noise.
* `GNSS_COURSE` (and a receiver's course) is horizontal ground movement. It does not show the CanSat's vertical falling
  direction, so it is not used to predict or adjust the landing point.
* If IMPACT is reported while the CanSat is still moving (bouncing, rolling, wind-dragged canopy), the lock may be early;
  use **Reset landing lock** and let it re-lock if the position looks wrong.
* The averaging is a plain mean of latitude/longitude, fine over metres, not intended for the 180th meridian.
* Directions are straight-line (great-circle); they do not account for obstacles, fences or terrain.
* NMEA is assumed to be ASCII with standard checksums; receivers that only emit proprietary sentences will not work.