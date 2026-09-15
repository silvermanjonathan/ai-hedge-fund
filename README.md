# AI Hedge Fund

This is a proof of concept for an AI-powered hedge fund. The goal of this project is to explore the use of AI to make trading decisions. This project is for **educational** purposes only and is not intended for real trading or investment.

> **🚧 The project is evolving.** We're rebuilding it into a persistent, always-on AI hedge fund — a *fund* as a first-class entity you can backtest, paper-trade, and (opt-in) run live, with the investor agents reimagined as pluggable, backtestable "alpha models." Read the **[Vision →](VISION.md)** and the **[Roadmap →](ROADMAP.md)**.

Note: the system does not actually make any trades.

[![Twitter Follow](https://img.shields.io/twitter/follow/virattt?style=social)](https://twitter.com/virattt)

## Disclaimer

This project is for **educational and research purposes only**.

- Not intended for real trading or investment
- No investment advice or guarantees provided
- Creator assumes no liability for financial losses
- Consult a financial advisor for investment decisions
- Past performance does not indicate future results

By using this software, you agree to use it solely for learning purposes.

## How to Install

```bash
pipx install aihf
```

(or `uv tool install aihf`, or `pip install aihf` into an environment of your choice)

Then run it from anywhere:

```bash
aihf
```

### API keys

The app asks for keys the first time it needs them and saves them to `~/.hedge-fund/.env` — nothing to configure up front. It needs:

- One LLM API key for the LLM-powered alpha models. Supported providers: Anthropic, OpenAI, DeepSeek, Google, xAI, Kimi.
- A data source. The default, `--data free` (or `HEDGE_FUND_DATA=free`), needs no paid key: fundamentals come from [SEC EDGAR](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)'s XBRL company facts and prices from Yahoo Finance via [yfinance](https://github.com/ranaroussi/yfinance) (Yahoo's data is for personal, educational use). The SEC requires every automated client to identify itself, so set `HEDGE_FUND_SEC_USER_AGENT="Your Name you@example.com"` — the app prompts for it. Alternatively `--data fd` uses a [Financial Datasets](https://financialdatasets.ai) API key for prices, fundamentals, and earnings.

Keys exported in your shell always win over the saved file.

The free source has no earnings-surprise data, so the `pead` model (the example mandate's earnings-drift strategy) needs `--data fd`; the CLI says so before running. Its fundamentals are dated by the 10-Q/10-K that first reported them and never restated, which keeps a backtest point-in-time and its LLM cache stable. A Finviz Elite price export could replace yfinance behind the `PriceSource` seam in `hedge_fund/data/prices.py`.

## How to Run

### Interactive app

```bash
aihf
```

With no arguments, this launches the interactive terminal app. Build a fund — pick stocks, strategies, rebalance cadence — or backtest a saved fund and watch its equity curve draw against its benchmark. Funds you build are saved as mandate files in `~/.hedge-fund/mandates/`.

### Non-interactive

Run one fund cycle from a mandate file. The full cycle record prints to stdout as JSON; a short human summary goes to stderr:

```bash
aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT
```

Backtest the mandate over history at its rebalance cadence:

```bash
aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT --backtest
```

A mandate is the desk — strategies, staff, risk, capital, cadence — and never names tickers; `--tickers` says what to point it at for this run.

## Development

```bash
git clone https://github.com/virattt/ai-hedge-fund.git
cd ai-hedge-fund
poetry install
poetry run aihf
poetry run pytest hedge_fund
```

### Analysts

Five personas reason as named investors — Buffett, Munger, Graham, Lynch, Druckenmiller (stylized approximations, not the individuals, not endorsements) — plus the `pead` quant model. Thirteen more are written as **schools, not impersonations**: each prompt is "an analyst applying X's published framework", carries a scope note on what its real-world method uses that the snapshot cannot supply, and names the school in its reasoning voice. Long-biased: `fisher`, `greenblatt` (Magic Formula), `fundsmith`, `pabrai` (Dhandho), `damodaran`, `klarman`, `schloss`, `dreman` (contrarian), `akre`, `quality_compounder` (a composite). Bearish or neutral lenses, because everything else is long-biased: `chanos` (forensic short), `earnings_quality_skeptic` (a composite), `dalio_resilience` (Dalio-*inspired*; he publishes no stock-selection method). Library strategies that staff them: `quality-compounders`, `contrarian-value`, `magic-formula`, `forensic-short`.

`aihf-universe <preset> --limit N` picks a ticker universe from a Finviz Elite screen (needs `FINVIZ_AUTH_TOKEN`) and, by default, drops names EDGAR cannot serve — no CIK, or no 10-K/10-Q on file, such as foreign private issuers on 20-F — replacing them with the next in export order; `--no-edgar-check` skips that. Compose it as `--tickers "$(poetry run aihf-universe quality --limit 10)"`.

### Anthropic models use the SDK directly

Claude models — `claude-fable-5-1` (the default), Opus 5, Sonnet 5, and any unlisted `claude-*` id — go through the official `anthropic` SDK rather than LangChain: the API enforces the analyst JSON schema as structured output, the persona system prompt carries a prompt-cache breakpoint, and adaptive thinking is steered by effort. Every other provider stays on LangChain.

- `--effort low|medium|high|xhigh|max` (or `HEDGE_FUND_LLM_EFFORT`) sets how hard the model thinks; the default is `high`. A mandate can pin it per model with `params: {effort: medium}`.
- `HEDGE_FUND_LLM_WORKERS` (default 4) is how many analyst calls run at once within a cycle; `1` runs them serially, with an identical record.
- Fable 5.1 is the most expensive tier. The disk cache under `~/.hedge-fund/cache/llm/` is what keeps backtests cheap: an unchanged snapshot never pays for a second call.
- A refusal (`stop_reason: "refusal"`) makes the agent abstain, like any other LLM failure. There is no fallback model.
- Each call logs model, effort, stop reason, and token counts — including cache reads and writes — at INFO.

### Data sources

`--data free|fd` (or `HEDGE_FUND_DATA`) selects the market data source; `free` is the default. `--refresh-data` (or `HEDGE_FUND_DATA_REFRESH=1`) ignores that cache for one run and rewrites it, for when a cached answer has gone stale. Each source keeps its own disk cache under `~/.hedge-fund/cache/` (`data-free/`, `data/`), and the raw EDGAR and Yahoo payloads are cached beside them with a one-day TTL, so a ticker's facts download once a day. All EDGAR traffic goes through one process-wide rate limiter (8 requests/second, under the SEC's 10) with the required `User-Agent`. Methods the free source cannot serve — news, insider trades, earnings — raise `NotImplementedError` rather than returning empty. A company that reorganised under a new CIK (a holding-company redomiciliation, say) keeps its history: the client reads the successor's Form 8-K12B, resolves the predecessor it names through EDGAR company search, and merges the predecessor's facts in.

## Weekly loop

`~/.hedge-fund/weekly.sh` is the desk's week in one script: it re-selects the quality and value universes from current Finviz values (`aihf-universe`), runs the quality desk, the value desk, and a resilience check (the `dalio_resilience` lens alone, over the union of both universes) at low effort, ingests the three records into the verdict ledger, and prints the candidates and the 63-day scorecard. Everything goes to `~/.hedge-fund/logs/weekly-<date>.log`.

- **The ledger** (`aihf-ledger ingest`) logs each distinct verdict once — identity is (school, ticker, snapshot hash) — with the close on the day it was first made and SPY the same day. A school re-reasons only when a filing changes, so a week with no new filings costs nothing: every verdict is a cache hit and the ledger adds no rows.
- **The scorecard** (`aihf-ledger scorecard`) grades each school per horizon (21, 63, 126 trading days) on excess return over SPY signed by the call, and against the equal-weight return of the names it saw that day. Below 20 scored calls it reads `provisional`; nothing is old enough to score until the first horizon elapses.
- **The playbook** (`aihf-ledger candidates`) combines the latest verdicts per ticker, restricted to schools reasoning on the same filing, through a few rules (consensus, contrarian, `resilience_confirmed` = resilient balance sheet plus schools bullish, and a forensic warning tag). The output is a list for review, written to `~/.hedge-fund/ledger/candidates-<date>.csv`. It is not orders, not advice.

Because the universes are re-selected weekly from a current screen, a name can enter or leave between runs; the ledger keeps every verdict ever made regardless.

To run it unattended on macOS, `scripts/com.aihf.weekly.plist` is a LaunchAgent template: fill in `__HOME__` and `__POETRY_BIN__` (the directory `command -v poetry` lives in), copy it to `~/Library/LaunchAgents/`, and `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aihf.weekly.plist`. It runs `~/.hedge-fund/weekly.sh` every Monday at 10:00 and writes to `~/.hedge-fund/logs/launchd.out.log` and `launchd.err.log`; `launchctl kickstart -k gui/$(id -u)/com.aihf.weekly` runs it once immediately. The wrapper at `~/.hedge-fund/weekly.sh` exports `AIHF_REPO` and execs `scripts/weekly.sh`, so the repo script is the only copy.

## How to Contribute

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

**Important**: Please keep your pull requests small and focused. This will make it easier to review and merge.

## Feature Requests

If you have a feature request, please open an [issue](https://github.com/virattt/ai-hedge-fund/issues) and make sure it is tagged with `enhancement`.

## License

This project is licensed under the MIT License - see the LICENSE file for details.
