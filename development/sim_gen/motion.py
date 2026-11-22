"""Differential-drive motion model for the simulated AMR.

Everything here is a pure function of the numpy `Generator` handed in, so two
runs with the same seed produce bit-identical trajectories.

The model is deliberately simple but *self-consistent*: the same velocity
arrays drive `/cmd_vel`, `/odom`, `/joint_states`, `/tf` and `/imu/data`, which
is what makes the fixture usable as ground truth for the AMR kinematic
plausibility checks -- a good episode has cmd_vel, odom and
wheel velocities that agree, and the deliberately-bad episode breaks that
agreement in exactly one documented way (`stationary_window`).
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

#: Internal integration rate. Every published topic rate (5/10/20/50 Hz)
#: divides this exactly, so a sample time maps to an exact array index.
FINE_HZ = 200.0

WHEEL_RADIUS_M = 0.1016
WHEEL_SEPARATION_M = 0.42
GRAVITY_MPS2 = 9.80665

#: First-order lag between commanded and achieved velocity (a real AMR does
#: not reach a commanded velocity instantly).
VELOCITY_TAU_S = 0.30
#: How often the commanded velocity profile picks a new target.
WAYPOINT_PERIOD_S = 2.5
MAX_LINEAR_MPS = 0.8
MAX_ANGULAR_RPS = 0.6


def _smoothstep(u: np.ndarray) -> np.ndarray:
    return u * u * (3.0 - 2.0 * u)


def _waypoint_profile(
    rng: np.random.Generator, n_fine: int, lo: float, hi: float
) -> np.ndarray:
    """A smooth piecewise profile: random waypoints, smoothstep-interpolated.

    Continuous and C1-ish, so the derived IMU accelerations are not full of
    step discontinuities.
    """
    t = np.arange(n_fine) / FINE_HZ
    n_wp = int(np.floor(t[-1] / WAYPOINT_PERIOD_S)) + 3 if n_fine else 3
    wp = rng.uniform(lo, hi, size=n_wp)
    idx = np.floor(t / WAYPOINT_PERIOD_S).astype(int)
    frac = _smoothstep((t - idx * WAYPOINT_PERIOD_S) / WAYPOINT_PERIOD_S)
    return wp[idx] * (1.0 - frac) + wp[idx + 1] * frac


class MotionModel:
    """Sampled trajectory for one episode.

    Attributes are numpy arrays indexed at `FINE_HZ`; use `index(t)` to get the
    array index for an episode-relative time in seconds.

    `stationary_window` is the deliberately-bad knob: inside it the *achieved*
    velocity is forced to zero (so odom, joint_states, tf and imu all report a
    parked robot) while `/cmd_vel` keeps commanding motion.
    """

    def __init__(
        self,
        duration_s: float,
        rng: np.random.Generator,
        stationary_window: Optional[Tuple[float, float]] = None,
    ) -> None:
        self.duration_s = float(duration_s)
        n = int(round(self.duration_s * FINE_HZ)) + 1
        dt = 1.0 / FINE_HZ

        self.t = np.arange(n) / FINE_HZ
        self.v_cmd = _waypoint_profile(rng, n, 0.05, MAX_LINEAR_MPS)
        self.w_cmd = _waypoint_profile(rng, n, -MAX_ANGULAR_RPS, MAX_ANGULAR_RPS)

        v_noise = rng.normal(0.0, 0.004, size=n)
        w_noise = rng.normal(0.0, 0.006, size=n)

        v = np.empty(n)
        w = np.empty(n)
        alpha = dt / VELOCITY_TAU_S
        cv = 0.0
        cw = 0.0
        for i in range(n):
            cv += (float(self.v_cmd[i]) - cv) * alpha
            cw += (float(self.w_cmd[i]) - cw) * alpha
            v[i] = cv + v_noise[i]
            w[i] = cw + w_noise[i]

        if stationary_window is not None:
            lo, hi = stationary_window
            self.stationary_mask = (self.t >= lo) & (self.t < hi)
            v[self.stationary_mask] = 0.0
            w[self.stationary_mask] = 0.0
        else:
            self.stationary_mask = np.zeros(n, dtype=bool)

        self.v = v
        self.w = w

        # Pose by forward Euler at FINE_HZ (pose[0] == origin).
        self.theta = np.concatenate(([0.0], np.cumsum(w[:-1]) * dt))
        self.x = np.concatenate(([0.0], np.cumsum(v[:-1] * np.cos(self.theta[:-1])) * dt))
        self.y = np.concatenate(([0.0], np.cumsum(v[:-1] * np.sin(self.theta[:-1])) * dt))
        self.distance = np.concatenate(([0.0], np.cumsum(np.abs(v[:-1])) * dt))

        # Body-frame proper acceleration: forward = dv/dt, lateral = v*w
        # (centripetal), plus gravity on z. This is what an IMU on the base
        # link would actually report.
        self.accel_x = np.gradient(v, dt) + rng.normal(0.0, 0.02, size=n)
        self.accel_y = v * w + rng.normal(0.0, 0.02, size=n)
        self.accel_z = GRAVITY_MPS2 + rng.normal(0.0, 0.02, size=n)
        self.gyro_x = rng.normal(0.0, 0.003, size=n)
        self.gyro_y = rng.normal(0.0, 0.003, size=n)
        self.gyro_z = w + rng.normal(0.0, 0.003, size=n)

        # Differential-drive wheels, exactly consistent with (v, w).
        v_left = v - w * WHEEL_SEPARATION_M / 2.0
        v_right = v + w * WHEEL_SEPARATION_M / 2.0
        self.wheel_vel_left = v_left / WHEEL_RADIUS_M
        self.wheel_vel_right = v_right / WHEEL_RADIUS_M
        self.wheel_pos_left = np.concatenate(
            ([0.0], np.cumsum(self.wheel_vel_left[:-1]) * dt)
        )
        self.wheel_pos_right = np.concatenate(
            ([0.0], np.cumsum(self.wheel_vel_right[:-1]) * dt)
        )

    def index(self, t_s: float) -> int:
        """Array index for an episode-relative time in seconds."""
        i = int(round(t_s * FINE_HZ))
        return max(0, min(i, self.t.size - 1))
