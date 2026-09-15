# aihf — conventions for Claude Code

- Educational simulator only. Nothing here places real trades; never add
  brokerage credentials or live-order code.
- The LLM's influence ends at Signal. blend_signals, apply_limits, and
  build_orders are pure and deterministic — never move sizing or ordering
  into a prompt.
- Point-in-time discipline: an analyst may only see data filed on or before
  as_of. FundamentalsSnapshot.render() is date-free on purpose.
- Failure contract (hedge_fund/signals/llm_agent.py): data-layer errors
  propagate; LLM call/parse/refusal errors abstain.
- Default LLM: claude-fable-5-1 via the anthropic SDK. Effort via
  HEDGE_FUND_LLM_EFFORT or --effort. Other providers stay on LangChain.
- Data source: HEDGE_FUND_DATA=free|fd (--data), default free. free = EDGAR
  companyfacts (first-reported values; filing_date is the fact's `filed`)
  + yfinance prices; every EDGAR call goes through the shared rate limiter
  in hedge_fund/data/edgar.py and needs HEDGE_FUND_SEC_USER_AGENT. Methods a
  source cannot serve raise NotImplementedError, never return empty.
- Tests live next to the code as test_*.py; fakes, no network.
- Check: poetry run pytest hedge_fund && poetry run black --check hedge_fund
  && poetry run isort --check-only hedge_fund && poetry run flake8 hedge_fund
