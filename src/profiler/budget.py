"""Execution budget management module.

Responsibility: Enforces request limits, USD caps, and priority allocations:
- priority "core": up to 90% of max_requests
- priority "enrich": up to 75% of max_requests
- priority "web": up to 60% of max_requests
- USD cap: $10.00
Raises BudgetExceeded cleanly when budget limits are breached.
"""

import math
from typing import Dict


class BudgetExceeded(Exception):
    """Raised cleanly when request budget, USD cap, or priority allocation limit is exceeded."""

    pass


class Budget:
    """Budget tracker enforcing request priority ceilings and USD cost caps."""

    PRIORITY_CAPS: Dict[str, float] = {
        "core": 0.90,    # Core may use up to 90%
        "enrich": 0.75,  # Enrich may use up to 75%
        "web": 0.60,     # Web may use up to 60%
    }

    def __init__(
        self,
        max_requests: int = 2000,
        stop_fraction: float = 0.9,
        max_usd: float = 10.0,
    ) -> None:
        self.max_requests = max_requests
        self.stop_fraction = stop_fraction
        self.max_usd = max_usd
        self.requests_spent = 0
        self.usd_spent = 0.0
        self.llm_tokens = 0
        self.llm_input_tokens = 0
        self.llm_output_tokens = 0
        self.llm_thoughts_tokens = 0
        self.llm_cost_usd = 0.0

    def add_llm_usage(
        self,
        prompt_tokens: int = 0,
        candidates_tokens: int = 0,
        thoughts_tokens: int = 0,
        cost_usd: float = 0.0,
        *,
        output_tokens: int | None = None,
        input_tokens: int | None = None,
    ) -> None:
        """Record LLM token usage and USD cost via a single unified path."""
        p_tokens = prompt_tokens if input_tokens is None else input_tokens
        if output_tokens is not None:
            out_tokens = output_tokens
        else:
            out_tokens = candidates_tokens + thoughts_tokens

        self.llm_input_tokens += p_tokens
        self.llm_output_tokens += out_tokens
        self.llm_thoughts_tokens += thoughts_tokens
        self.llm_tokens += (p_tokens + out_tokens)

        if cost_usd > 0.0:
            self.spend_usd(cost_usd)
            self.llm_cost_usd += cost_usd

    def record_tokens(self, tokens: int) -> None:
        """Record spent LLM tokens."""
        self.llm_tokens += tokens

    def remaining(self) -> int:
        """Return remaining requests available up to max_requests."""
        return max(0, self.max_requests - self.requests_spent)

    def can_afford(self, n: int = 1, priority: str = "core") -> bool:
        """Check if spending n requests for priority is allowed by ceilings and caps."""
        cap_fraction = self.PRIORITY_CAPS.get(priority.lower(), self.stop_fraction)
        if cap_fraction >= 0.9:
            max_allowed_for_priority = math.ceil(self.max_requests * cap_fraction)
        else:
            max_allowed_for_priority = int(self.max_requests * cap_fraction)

        if self.requests_spent + n > max_allowed_for_priority:
            return False
        if self.requests_spent + n > self.max_requests:
            return False
        if self.usd_spent >= self.max_usd:
            return False
        return True

    def spend(self, n: int = 1, priority: str = "core") -> None:
        """Record spending n requests for the given priority level.

        Raises:
            BudgetExceeded: If requested spending breaches priority allocation ceiling or total max.
        """
        if not self.can_afford(n, priority):
            cap_frac = self.PRIORITY_CAPS.get(priority.lower(), self.stop_fraction)
            if cap_frac >= 0.9:
                max_allowed = math.ceil(self.max_requests * cap_frac)
            else:
                max_allowed = int(self.max_requests * cap_frac)
            raise BudgetExceeded(
                f"Budget limit exceeded for priority '{priority}': spent={self.requests_spent}, "
                f"requested={n}, priority_max={max_allowed}, total_max={self.max_requests}"
            )
        self.requests_spent += n

    def spend_usd(self, amount: float) -> None:
        """Record spending USD cost against max_usd cap.

        Raises:
            BudgetExceeded: If requested amount breaches max_usd.
        """
        if amount < 0:
            raise ValueError("Cost amount cannot be negative.")
        if self.usd_spent + amount > self.max_usd:
            raise BudgetExceeded(
                f"USD budget limit exceeded: spent=${self.usd_spent:.4f}, "
                f"requested=${amount:.4f}, max=${self.max_usd:.2f}"
            )
        self.usd_spent += amount

    # Backward compatibility helpers
    def record_request(self) -> None:
        self.spend(1, priority="core")

    def record_cost(self, amount: float) -> None:
        self.spend_usd(amount)

    def record_company(self) -> None:
        pass

    def check_budget(self) -> None:
        if self.requests_spent >= self.max_requests:
            raise BudgetExceeded("Max requests limit reached.")
        if self.usd_spent >= self.max_usd:
            raise BudgetExceeded("Max USD cost limit reached.")

    def summary(self) -> dict:
        return {
            "requests_spent": self.requests_spent,
            "max_requests": self.max_requests,
            "usd_spent": round(self.usd_spent, 4),
            "max_usd": self.max_usd,
            "remaining": self.remaining(),
            "llm_tokens": {
                "input_tokens": self.llm_input_tokens,
                "output_tokens": self.llm_output_tokens,
                "thoughts_tokens": self.llm_thoughts_tokens,
                "total": self.llm_tokens,
            },
        }


# Aliases for backward compatibility
BudgetTracker = Budget
BudgetExceededError = BudgetExceeded
