import abc
import cvxpy as cp
import numpy as np
from cbf_opt import Dynamics, ControlAffineDynamics
from cbf_opt import CBF, ControlAffineCBF, ImplicitCBF, ControlAffineImplicitCBF, BackupController
from typing import Dict, Optional
from cbf_opt.tests import test_asif

import logging

logger = logging.getLogger(__name__)

# Array = np.ndarray or torch.tensor
Array = np.ndarray
# batched_ncbf = lambda x, y: torch.bmm(x, y)
batched_cbf = lambda x, y: np.einsum("ijk,ikl->ijl", x, y)
single_cbf = lambda x, y: x @ y


class ASIF(metaclass=abc.ABCMeta):
    def __init__(self, dynamics: Dynamics, cbf: CBF, test: bool = True, **kwargs) -> None:
        self.dynamics = dynamics
        self.cbf = cbf
        self.nominal_control = None
        self.alpha = kwargs.get("alpha", lambda x: x)
        self.verbose = kwargs.get("verbose", False)
        self.solver = kwargs.get("solver", "OSQP")
        self.nominal_policy = kwargs.get("nominal_policy", lambda x, t: np.zeros(self.dynamics.control_dims))
        self.controller_dt = kwargs.get("controller_dt", self.dynamics.dt)
        if test:
            test_asif.test_asif(self)

    def set_nominal_control(self, state: Array, time: float = 0.0, nominal_control: Optional[Array] = None) -> None:
        if nominal_control is not None:
            # TODO: can we just get  rid of this?
            assert isinstance(nominal_control, Array) and nominal_control.shape[-1] == self.dynamics.control_dims
            self.nominal_control = nominal_control
        else:
            self.nominal_control = self.nominal_policy(state, time)

    @abc.abstractmethod
    def __call__(self, state: Array, time: float = 0.0, nominal_control: Optional[Array] = None) -> Array:
        """Implements the active safety invariance filter"""

    def save_info(self, state: Array, control: Array, time: float = 0.0) -> Dict:
        return {"unsafe": self.cbf.is_unsafe(state, time)}

    def save_measurements(self, state: Array, control: Array, time: float = 0.0) -> Dict:
        dict = (
            self.nominal_policy.save_measurements(state, control, time)
            if hasattr(self.nominal_policy, "save_measurements")
            else {}
        )
        dict["vf"] = self.cbf.vf(state, time)
        return dict


class ControlAffineASIF(ASIF):
    def __init__(self, dynamics: ControlAffineDynamics, cbf: ControlAffineCBF, test: bool = True, **kwargs) -> None:
        super().__init__(dynamics, cbf, test, **kwargs)
        self.filtered_control = cp.Variable(self.dynamics.control_dims)
        self.nominal_control_cp = cp.Parameter(self.dynamics.control_dims)

        self.umin = kwargs.get("umin")
        self.umax = kwargs.get("umax")
        self.b = cp.Parameter((1,))
        self.A = cp.Parameter((1, self.dynamics.control_dims))

        self.opt_sol = np.zeros(self.filtered_control.shape)

        if test:
            test_asif.test_control_affine_asif(self)

    def setup_optimization_problem(self):
        """
        min || u - u_des ||^2
        s.t. A @ u + b >= 0
        """
        self.obj = cp.Minimize(
            cp.quad_form(self.filtered_control - self.nominal_control_cp, np.eye(self.dynamics.control_dims))
        )
        self.constraints = [self.A @ self.filtered_control + self.b >= 0]
        if self.umin is not None:
            self.constraints.append(self.filtered_control >= self.umin)
        if self.umax is not None:
            self.constraints.append(self.filtered_control <= self.umax)
        self.QP = cp.Problem(self.obj, self.constraints)
        assert self.QP.is_qp(), "This is not a quadratic program"

    def set_constraint(self, Lf_h: Array, Lg_h: Array, h: float):
        self.b.value = np.atleast_1d(self.alpha(h) + Lf_h)
        self.A.value = np.atleast_2d(Lg_h)

    def __call__(self, state: Array, time: float = 0.0, nominal_control=None) -> Array:
        if not hasattr(self, "QP"):
            self.setup_optimization_problem()
        self.set_nominal_control(state, time, nominal_control)
        return self.u(state, time)

    def u(self, state: Array, time: float = 0.0):
        h = np.atleast_1d(self.cbf.vf(state, time))
        Lf_h, Lg_h = self.cbf.lie_derivatives(state, time)  # TODO: Shouldn't Lf_h be a float?
        opt_sols = np.zeros_like(self.nominal_control)
        if state.ndim == 1:
            state = state[None, ...]
        for i in range(state.shape[0]):
            self.set_constraint(Lf_h[i], Lg_h[i], h[i])
            self.nominal_control_cp.value = np.atleast_1d(self.nominal_control[i])
            self._solve_problem()
            opt_sols[i] = self.opt_sol

        return opt_sols

    def _solve_problem(self):
        """Lower level function to solve the optimization problem"""
        solver_failure = False
        try:
            val = self.QP.solve(solver=self.solver, verbose=self.verbose)
            if val == np.inf:
                solver_failure = True
            else:
                self.opt_sol = self.filtered_control.value
        except (cp.SolverError, ValueError):
            solver_failure = True
        if self.QP.status in ["infeasible", "unbounded"] or solver_failure:
            logger.warning("QP solver failed")
            if (self.umin is None) and (self.umax is None):
                logger.warning("Returning nominal control value, but this should not happen")
                self.opt_sol = self.nominal_control_cp.value
            else:
                umin = self.umin if self.umin is not None else -np.inf
                umax = self.umax if self.umax is not None else np.inf
                QP_wout_constraints = cp.Problem(self.obj, self.constraints[0:1])
                try:
                    val = QP_wout_constraints.solve(solver=self.solver, verbose=self.verbose)
                    if val == np.inf:
                        solver_failure = True
                    else:
                        self.opt_sol = np.clip(self.filtered_control.value, umin, umax)
                except (cp.SolverError, ValueError):
                    solver_failure = True
                if QP_wout_constraints.status in ["infeasible", "unbounded"] or solver_failure:
                    logger.warning("QP solver failed even without input constraints")
                    logger.warning("Returning nominal control value, but this should not happen")
                    self.opt_sol = self.nominal_control_cp.value

                # if self.umin is not None and self.umax is not None:
                #     # TODO: This should depend on "controlMode"
                #     logger.warning("Returning safest possible control")
                #     self.opt_sol = (
                #         np.int64(self.A.value >= 0) * self.umax + np.int64(self.A.value < 0) * self.umin
                #     ).reshape(-1)
                # elif (self.A.value >= 0).all() and self.umax is not None:
                #     logger.warning("Returning umax")
                #     self.opt_sol = self.umax
                # elif (self.A.value <= 0).all() and self.umin is not None:
                #     logger.warning("Returning umin")
                #     self.opt_sol = self.umin
                # else:
                #     logger.warning("Returning nominal control value")
                #     self.opt_sol = self.nominal_control_cp.value
                # elif self.umax is not None:
                #     self.opt_sol = (
                #         np.int64(self.A.value >= 0) * self.umax
                #         + np.int64(self.A.value < 0) * self.nominal_control_cp.value
                #     ).reshape(-1)
                # elif self.umin is not None:
                #     self.opt_sol = (
                #         np.int64(self.A.value >= 0) * self.nominal_control_cp.value
                #         + np.int64(self.A.value < 0) * self.umin
                #     ).reshape(-1)


class RelaxedControlAffineASIF(ControlAffineASIF):
    def __init__(self, dynamics: ControlAffineDynamics, cbf: ControlAffineCBF, test: bool = True, **kwargs) -> None:
        super().__init__(dynamics, cbf, test, **kwargs)
        self.slack_variable = cp.Variable(1)

        self.slack_penalty = kwargs.get("slack_penalty")

    def setup_optimization_problem(self):
        """
        min || u - u_des ||^2 + c mu^2
        s.t. A @ u + b >= -mu
             mu >= 0
        """
        self.obj = cp.Minimize(
            cp.quad_form(self.filtered_control - self.nominal_control_cp, np.eye(self.dynamics.control_dims)) + self.slack_penalty*self.slack_variable**2
        )
        self.constraints = [self.A @ self.filtered_control + self.b + self.slack_variable>= 0]
        if self.umin is not None:
            self.constraints.append(self.filtered_control >= self.umin)
        if self.umax is not None:
            self.constraints.append(self.filtered_control <= self.umax)
        if self.slack_penalty is not None:
            self.constraints.append(self.slack_variable >= 0)
        self.QP = cp.Problem(self.obj, self.constraints)
        assert self.QP.is_qp(), "This is not a quadratic program"

    def set_constraint(self, Lf_h: Array, Lg_h: Array, h: float):
        super().set_constraint(Lf_h, Lg_h,h)

    def __call__(self, state: Array, time: float = 0.0, nominal_control=None) -> Array:
        if not hasattr(self, "QP"):
            self.setup_optimization_problem()
        self.set_nominal_control(state, time, nominal_control)
        return self.u(state, time)

    def u(self, state: Array, time: float = 0.0):
        h = np.atleast_1d(self.cbf.vf(state, time))
        Lf_h, Lg_h = self.cbf.lie_derivatives(state, time)  # TODO: Shouldn't Lf_h be a float?
        opt_sols = np.zeros_like(self.nominal_control)
        if state.ndim == 1:
            state = state[None, ...]
        for i in range(state.shape[0]):
            self.set_constraint(Lf_h[i], Lg_h[i], h[i])
            self.nominal_control_cp.value = np.atleast_1d(self.nominal_control[i])
            self._solve_problem()
            opt_sols[i] = self.opt_sol

        return opt_sols

    def _solve_problem(self):
        """Lower level function to solve the optimization problem"""
        solver_failure = False
        try:
            val = self.QP.solve(solver=self.solver, verbose=self.verbose)
            if val == np.inf:
                solver_failure = True
            else:
                self.opt_sol = self.filtered_control.value
                breakpoint()
        except (cp.SolverError, ValueError):
            solver_failure = True
        if self.QP.status in ["infeasible", "unbounded"] or solver_failure:
            logger.warning("QP solver failed")
            logger.warning("Returning nominal control value, but this should not happen")
            self.opt_sol = self.nominal_control_cp.value

#class RelaxedControlAffineASIF(ControlAffineASIF):
#    def __init__(self, dynamics: ControlAffineDynamics, cbf: ControlAffineCBF, test: bool = True, **kwargs) -> None:
#        super().__init__(dynamics, cbf, test, **kwargs)

class ImplicitASIF(metaclass=abc.ABCMeta):
    def __init__(self, dynamics: Dynamics, cbf: ImplicitCBF, backup_controller: BackupController, **kwargs) -> None:
        self.dynamics = dynamics
        self.cbf = cbf
        self.nominal_control = None
        self.backup_controller = backup_controller
        self.verify_every_x = kwargs.get("verify_every_x", 1)
        self.n_backup_steps = int(self.backup_controller.T_backup / self.dynamics.dt)
        self.alpha_backup = kwargs.get("alpha_backup", lambda x: x)
        self.alpha_safety = kwargs.get("alpha_safety", lambda x: x)
        self.nominal_policy = kwargs.get("nominal_policy", lambda x, t: 0)

    @abc.abstractmethod
    def __call__(self, state: Array, nominal_control=None, time: float = 0.0) -> Array:
        """Implements the active safety invariance filter"""


# TO TEST
class ImplicitControlAffineASIF(ImplicitASIF):
    def __init__(
        self,
        dynamics: ControlAffineDynamics,
        cbf: ControlAffineImplicitCBF,
        backup_controller: BackupController,
        **kwargs
    ) -> None:
        super().__init__(dynamics, cbf, backup_controller, **kwargs)

        assert isinstance(self.dynamics, ControlAffineDynamics)
        assert isinstance(self.cbf, ControlAffineImplicitCBF)
        self.filtered_control = cp.Variable(self.dynamics.control_dims)
        self.nominal_control_cp = cp.Parameter(self.dynamics.control_dims)
        self.b = cp.Parameter((int(self.n_backup_steps / self.verify_every_x) + 2,))
        self.A = cp.Parameter((int(self.n_backup_steps / self.verify_every_x) + 2, self.dynamics.control_dims))
        """
        min || u - u_des ||^2
        s.t. A @ u + b >= 0
        """
        self.obj = cp.Minimize(cp.norm(self.filtered_control - self.nominal_control_cp, 2) ** 2)
        self.constraints = [self.A @ self.filtered_control + self.b >= 0]
        # TODO: Add umin and umax
        self.QP = cp.Problem(self.obj, self.constraints)

    def __call__(self, state: Array, time: float = 0.0, nominal_control=None) -> Array:
        # grad_safety = self.cbf._grad_safety_vf(state, time)  # TODO: change to not have _grad_vf
        # grad_backup = self.cbf._grad_backup_vf(state, time)  # TODO: change to not have _grad_vf
        states, Qs, times = self.backup_controller.rollout_backup_w_sensitivity_matrix(state, time)
        A = np.zeros(self.A.shape)
        b = np.zeros(self.b.shape)

        for i in range(self.n_backup_steps // self.verify_every_x):
            idx = i * self.verify_every_x
            b[i], A[i] = self.cbf.lie_derivatives(
                states[idx], Qs[idx], times[idx]
            )  # TODO: Fix why it shows error -> Do I use wrong terminology for super classes?
            b[i] += self.alpha_safety(self.cbf.safety_vf(states[idx], times[idx]))

        b[-2], A[-2] = self.cbf.lie_derivatives(states[-1], Qs[-1], times[-1])
        b[-2] += self.alpha_backup(self.cbf.safety_vf(states[-1], times[-1]))
        b[-1], A[-1] = self.cbf.lie_derivatives(
            states[-1], Qs[-1], times[-1], backup_set=True
        )  # TODO: Discuss with Yuxiao if there is an additional term for the nonbackup set -> YES TO IMPLEMENT
        b[-1] += self.alpha_backup(self.cbf.backup_vf(states[-1], times[-1]))

        self.A.value = A
        self.b.value = b

        if nominal_control is not None:
            assert isinstance(nominal_control, Array) and nominal_control.shape == (self.dynamics.control_dims,)
            self.nominal_control_cp.value = nominal_control
        else:
            self.nominal_control_cp.value = self.nominal_policy(state, time)

        self.QP.solve()
        # TODO: Relax solution if not solved with large penalty on constraints
        # TODO: Check if the solution is feasible
        return np.atleast_1d(self.filtered_control.value)


# TOTEST
class TradeoffFilter(ImplicitASIF):
    def __init__(self, dynamics: Dynamics, cbf: ImplicitCBF, backup_controller: BackupController, **kwargs):
        super().__init__(dynamics, cbf, backup_controller, **kwargs)
        self.beta = kwargs.get("beta", 10)
        self.decay_func = kwargs.get("decay_func", lambda x, t, h: 1 - np.exp(-self.beta * np.maximum(h, 0)))

    def __call__(self, state: Array, time: float = 0.0, nominal_control: Optional[Array] = None) -> Array:
        assert self.decay_func is not None, "Decay function must be specified"
        h_curr = self.cbf.vf(state, time)
        filter_rate = self.decay_func(state, time, h_curr)

        if nominal_control is not None:
            assert isinstance(nominal_control, Array) and nominal_control.shape == (self.dynamics.control_dims,)
        else:
            nominal_control = self.nominal_policy(state, time)

        return np.atleast_1d(
            filter_rate * nominal_control + (1 - filter_rate) * self.backup_controller.policy(state, time)
        )


# FIXME: Not for this code base
class GeneralizedASIF(ASIF):
    def __init__(self, dynamics: Dynamics, cbf: CBF, **kwargs) -> None:
        super().__init__(dynamics, cbf, **kwargs)
        self.beta0 = kwargs.get("beta0", 0.0)
        self.penalty_coeff = kwargs.get("penalty_coeff", 1.0)


# TOTEST
# FIXME: Not for this code base
class GeneralizedControlAffineASIF(GeneralizedASIF, ControlAffineASIF):
    def __init__(self, dynamics: ControlAffineDynamics, cbf: ControlAffineCBF, **kwargs) -> None:
        super().__init__(dynamics, cbf, **kwargs)
        self.beta = cp.Variable((1,))
        self.obj += cp.Minimize(self.penalty_coeff * (self.beta - self.beta0) ** 2)
        self.constraints[0] = self.A @ self.filtered_control + self.b >= self.beta

    def get_beta(self) -> float:
        return self.beta.value
