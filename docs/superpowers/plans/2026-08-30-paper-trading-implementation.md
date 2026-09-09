# v0.9 Forward Paper Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, forward-only paper-trading ledger for sh.510300 with isolated SMA 20/60 and momentum 120 accounts, next-bar-open simulated execution, durable audit history, and beginner-friendly Windows commands.

**Architecture:** A pure state-transition engine produces ordered event drafts. A DuckDB store appends hash-chained events and maintains a rebuildable current-state cache. A pipeline validates local market-data lineage, processes unseen bars atomically for both accounts, and renders deterministic reports.

**Tech Stack:** Python 3.11-3.13, pandas, DuckDB, matplotlib, argparse, pytest, Ruff, mypy, PowerShell.

## Global Constraints

- Do not connect to a broker, send orders, store credentials, or describe results as investment advice.
- Trade only the configured ETF `sh.510300` in v0.9.
- Use two isolated accounts: SMA 20/60 and momentum 120.
- Generate a signal after close on day D and execute only at the next validated bar's open.
- Default assumptions are CNY 100000 initial cash, 100 units per lot, 3 bps commission, CNY 5 minimum commission, 0 bps sell tax, and 2 bps one-way slippage.
- Fee and slippage values are hypothetical scenarios, not current legal or broker standards.
- Never backfill account performance before account creation.
- Append account events; never edit or delete committed event history.
- Reject stale data, data errors, unknown warnings, malformed update summaries, all-source failures, hash changes, invalid prices, and reconciliation failures.
- Allow `unadjusted_prices` and `mixed_sources` only as prominently disclosed warnings.
- A successful fallback source may proceed when the target update record has positive rows and a curated path; preserve every upstream error in lineage and reports.
- Every implementation task follows red-green-refactor, runs targeted tests, and ends with a commit.

## File Map

Create:

- `src/finance_lab/paper_models.py` — immutable configuration, state, order, event, context, and operation result types.
- `src/finance_lab/paper_engine.py` — pure signal and one-bar state transitions.
- `src/finance_lab/paper_lock.py` — cross-process file-handle lock.
- `src/finance_lab/paper_store.py` — schema, transactions, append, rebuild, and audit.
- `src/finance_lab/paper_pipeline.py` — data gate and init/run/status orchestration.
- `src/finance_lab/paper_report.py` — HTML/JSON/CSV/PNG outputs.
- `tests/paper_helpers.py` — deterministic temporary project fixture.
- `tests/test_paper_models.py`
- `tests/test_paper_engine.py`
- `tests/test_paper_lock.py`
- `tests/test_paper_store.py`
- `tests/test_paper_pipeline.py`
- `tests/test_paper_report.py`
- `tests/test_paper_cli.py`
- `scripts/run_paper_trading.ps1`
- `run_paper_trading.cmd`
- `docs/第九课.md`
- `experiments/008_forward_paper_trading.md`
- `docs/releases/v0.9.0.md`

Modify:

- `src/finance_lab/ledger.py` — expose validated fee and lot-sizing primitives without changing legacy results.
- `src/finance_lab/data_manifest.py` — expose a structured update-summary parser while preserving graceful manifest degradation.
- `src/finance_lab/cli.py` — add paper-init, paper-run, and paper-status.
- `README.md`, `CHANGELOG.md`, `pyproject.toml`, `src/finance_lab/__init__.py` — document and version v0.9.
- `scripts/package_release.ps1` — package and verify all v0.9 paths.

---

### Task 1: Public accounting primitives and immutable paper models

**Files:**
- Modify: `src/finance_lab/ledger.py`
- Create: `src/finance_lab/paper_models.py`
- Modify: `tests/test_ledger.py`
- Create: `tests/test_paper_models.py`

**Interfaces:**
- Produces: `validate_ledger_config(config: LedgerConfig) -> None`
- Produces: `commission_for(notional: float, config: LedgerConfig) -> float`
- Produces: `affordable_shares(cash: float, execution_price: float, config: LedgerConfig) -> int`
- Produces: `StrategySpec`, `PaperAccount`, `PendingOrder`, `PaperState`, `PaperEventDraft`, `PaperDataContext`, `PaperOperationResult`
- Consumes: existing `LedgerConfig`

- [ ] **Step 1: Write failing tests for public accounting helpers**

```python
def test_public_accounting_helpers_preserve_cash_and_lot_invariants() -> None:
    config = LedgerConfig(initial_cash=10_005, lot_size=100, minimum_commission=5)
    validate_ledger_config(config)
    shares = affordable_shares(10_005, 100.0, config)
    notional = shares * 100.0
    assert shares % 100 == 0
    assert notional + commission_for(notional, config) <= 10_005
```

- [ ] **Step 2: Run the new ledger test and confirm the import failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ledger.py::test_public_accounting_helpers_preserve_cash_and_lot_invariants -v`
Expected: FAIL because the three public helper names do not exist.

- [ ] **Step 3: Rename the private helpers and update every legacy call site**

Use these exact public signatures in `ledger.py`:

```python
def validate_ledger_config(config: LedgerConfig) -> None:
    finite_nonnegative = {
        "commission_bps": config.commission_bps,
        "minimum_commission": config.minimum_commission,
        "sell_tax_bps": config.sell_tax_bps,
        "slippage_bps": config.slippage_bps,
    }
    if not math.isfinite(config.initial_cash) or config.initial_cash <= 0:
        raise ValueError("initial_cash 必须是大于0的有限数字")
    if type(config.lot_size) is not int or config.lot_size <= 0:
        raise ValueError("lot_size 必须是大于0的整数")
    for name, value in finite_nonnegative.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} 必须是大于等于0的有限数字")
    if config.commission_bps >= 10_000 or config.sell_tax_bps >= 10_000:
        raise ValueError("费用比例必须小于10000 bps")
    if config.slippage_bps >= 10_000:
        raise ValueError("slippage_bps 必须小于10000")


def commission_for(notional: float, config: LedgerConfig) -> float:
    if notional <= 0:
        return 0.0
    return max(config.minimum_commission, notional * config.commission_bps / 10_000.0)


def affordable_shares(cash: float, execution_price: float, config: LedgerConfig) -> int:
    if cash <= config.minimum_commission or execution_price <= 0:
        return 0
    lots = int(cash // (execution_price * config.lot_size))
    while lots > 0:
        shares = lots * config.lot_size
        notional = shares * execution_price
        if notional + commission_for(notional, config) <= cash:
            return shares
        lots -= 1
    return 0
```

Replace all calls to the old underscored names. Do not change `run_account_ledger` outputs.

- [ ] **Step 4: Write model validation tests**

```python
def test_default_accounts_are_isolated_and_configuration_is_locked() -> None:
    accounts = make_default_accounts(
        portfolio_id="default",
        created_market_date=date(2026, 8, 28),
        ledger_config=LedgerConfig(),
    )
    assert [item.account_id for item in accounts] == [
        "default-sma-20-60-v1",
        "default-momentum-120-v1",
    ]
    assert accounts[0].config_hash != accounts[1].config_hash
    assert accounts[0].initial_cash == accounts[1].initial_cash == 100_000


@pytest.mark.parametrize("value", ["", "../escape", r"C:\escape", "Upper", "has space"])
def test_portfolio_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="账户组"):
        validate_portfolio_id(value)
```

- [ ] **Step 5: Implement frozen dataclasses and canonical hashes**

`paper_models.py` must define:

```python
PaperStrategyName = Literal["sma", "momentum"]
PaperAction = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class StrategySpec:
    name: PaperStrategyName
    short_window: int | None = None
    long_window: int | None = None
    momentum_lookback: int | None = None


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    signal_date: date
    action: PaperAction


@dataclass(frozen=True)
class PaperState:
    account_id: str
    last_trade_date: date
    cash: float
    shares: int
    last_close: float
    equity: float
    equity_peak: float
    drawdown: float
    last_target_position: int | None
    pending_order: PendingOrder | None
    last_event_hash: str


@dataclass(frozen=True)
class PaperEventDraft:
    event_type: str
    trade_date: date
    signal_date: date | None
    order_id: str | None
    action: PaperAction | None
    quantity: int
    reference_price: float | None
    execution_price: float | None
    notional: float
    commission: float
    tax: float
    slippage_cost: float
    cash_after: float
    shares_after: int
    close_price: float
    equity_after: float
    drawdown_after: float
    reason_code: str | None
    payload: dict[str, object]


@dataclass(frozen=True)
class EngineStep:
    state: PaperState
    events: tuple[PaperEventDraft, ...]
```

Also define `PaperAccount` with account ID, portfolio ID, symbol, instrument kind, strategy, LedgerConfig, created market date, engine version, and config hash; `PaperDataContext` with the full manifest/update lineage; and `PaperOperationResult` with status, portfolio ID, processed dates, current states, data context, and optional report path. Implement `validate_portfolio_id` with `re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)`, a full 64-character SHA-256 config hash, and `make_default_accounts` for the fixed strategy pair.

- [ ] **Step 6: Run targeted and legacy ledger tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_models.py tests/test_ledger.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/finance_lab/ledger.py src/finance_lab/paper_models.py tests/test_ledger.py tests/test_paper_models.py
git commit -m "feat(paper): define immutable account models"
```

### Task 2: Pure signal and one-bar execution engine

**Files:**
- Create: `src/finance_lab/paper_engine.py`
- Create: `tests/test_paper_engine.py`

**Interfaces:**
- Consumes: Task 1 models and accounting helpers.
- Produces: `signal_for_history(closes: pd.Series, spec: StrategySpec) -> int | None`
- Produces: `initialize_account(account: PaperAccount, history: pd.DataFrame) -> EngineStep`
- Produces: `advance_one_bar(account: PaperAccount, state: PaperState, history: pd.DataFrame) -> EngineStep`

- [ ] **Step 1: Write failing signal tests**

```python
def test_signal_warmup_is_not_silently_treated_as_cash() -> None:
    assert signal_for_history(pd.Series([100.0] * 59), StrategySpec("sma", 20, 60)) is None
    assert signal_for_history(pd.Series([100.0] * 120), StrategySpec("momentum", momentum_lookback=120)) is None


def test_future_prices_do_not_change_today_signal() -> None:
    prefix = pd.Series([float(value) for value in range(1, 122)])
    first = signal_for_history(prefix, StrategySpec("momentum", momentum_lookback=120))
    second = signal_for_history(pd.concat([prefix, pd.Series([9999.0])]).iloc[:-1], StrategySpec("momentum", momentum_lookback=120))
    assert first == second == 1
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_engine.py -v`
Expected: FAIL because `paper_engine` does not exist.

- [ ] **Step 3: Implement exact warmup and signal rules**

```python
def signal_for_history(closes: pd.Series, spec: StrategySpec) -> int | None:
    values = closes.astype(float)
    if values.empty or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("close 必须全部是有限正数")
    if spec.name == "sma":
        assert spec.short_window is not None and spec.long_window is not None
        if len(values) < spec.long_window:
            return None
        return int(values.iloc[-spec.short_window :].mean() > values.iloc[-spec.long_window :].mean())
    assert spec.momentum_lookback is not None
    if len(values) <= spec.momentum_lookback:
        return None
    return int(values.iloc[-1] / values.iloc[-1 - spec.momentum_lookback] - 1.0 > 0)
```

- [ ] **Step 4: Add failing next-bar, lot, fee, no-retry, and reconciliation tests**

Tests must assert:

```python
assert created.pending_order is not None
assert created.pending_order.signal_date == created.last_trade_date
assert advanced.events[0].event_type == "ORDER_FILLED"
assert advanced.state.shares % account.ledger_config.lot_size == 0
assert advanced.state.cash >= 0
assert advanced.state.equity == pytest.approx(
    advanced.state.cash + advanced.state.shares * float(history.iloc[-1]["close"])
)
assert all(event.signal_date < event.trade_date for event in advanced.events if event.event_type == "ORDER_FILLED")
```

Add a low-cash case that emits `ORDER_SKIPPED` once and does not recreate a buy while the target remains 1.

- [ ] **Step 5: Implement ordered event drafts and state transitions**

The transition order is fixed: pending order fill or skip, `VALUATION`, `SIGNAL_GENERATED`, optional `ORDER_CREATED`. Buy price is `open * (1 + slippage_bps / 10000)`; sell price is `open * (1 - slippage_bps / 10000)`. Compute a deterministic order ID from account ID, signal date, and action. Validate finite positive open/close, nonnegative cash and shares, integer lots, positive equity, and exact cash/equity reconciliation before returning `EngineStep`.

- [ ] **Step 6: Run engine tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_engine.py tests/test_ledger.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add src/finance_lab/paper_engine.py tests/test_paper_engine.py
git commit -m "feat(paper): add forward-only execution engine"
```

### Task 3: Cross-process run lock

**Files:**
- Create: `src/finance_lab/paper_lock.py`
- Create: `tests/test_paper_lock.py`

**Interfaces:**
- Produces: `paper_run_lock(path: Path) -> ContextManager[None]`
- Produces: `PaperLockError`

- [ ] **Step 1: Write a failing subprocess contention test**

Hold the lock in the pytest process, start a second Python process that calls the same context manager, and assert exit code 2 plus a Chinese “already running” message.

- [ ] **Step 2: Implement a standard-library file-handle lock**

Use `msvcrt.locking` on Windows and `fcntl.flock` on POSIX. Keep the handle open for the context lifetime and always unlock in `finally`. Do not use file existence as ownership.

- [ ] **Step 3: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_lock.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/paper_lock.py tests/test_paper_lock.py
git commit -m "feat(paper): prevent concurrent local runs"
```

### Task 4: DuckDB schema, atomic append, reconstruction, and audit

**Files:**
- Create: `src/finance_lab/paper_store.py`
- Create: `tests/test_paper_store.py`

**Interfaces:**
- Consumes: `PaperAccount`, `PaperState`, `EngineStep`, `PaperDataContext`
- Produces: `PaperStore(database: Path, read_only: bool = False)`
- Produces methods: `ensure_schema`, `portfolio_exists`, `initialize_portfolio`, `commit_batch`, `load_states`, `rebuild_state`, `audit_portfolio`, `portfolio_snapshot`

- [ ] **Step 1: Write failing schema tests**

Assert the database creates exactly `paper_schema_metadata`, `paper_accounts`, `paper_runs`, `paper_events`, and `paper_state`, and records schema version 1 without altering `daily_prices`.

- [ ] **Step 2: Implement schema version 1**

Use parameter-bound SQL. Required constraints are:

```sql
PRIMARY KEY (account_id)
UNIQUE (account_id, trade_date)
PRIMARY KEY (event_id)
UNIQUE (run_id, sequence_no)
CHECK (shares >= 0)
CHECK (cash >= 0)
```

Store JSON as canonical UTF-8 text with `sort_keys=True`, compact separators, and `allow_nan=False`.

- [ ] **Step 3: Write failing atomicity and idempotency tests**

Create both accounts in one transaction. Inject an exception while appending the second account and assert neither account has events for that date. Recommit the same account/date and assert a clear idempotency error with unchanged row counts.

- [ ] **Step 4: Implement append and state-cache updates**

For every event, hash the canonical event body with the previous event hash. The first event uses the account config hash as its previous value. Insert events, the committed run, and the derived state in one transaction per trade date.

- [ ] **Step 5: Write failing tamper and rebuild tests**

Update one event value directly with DuckDB, assert `audit_portfolio` fails, restore a clean database, and assert a full event replay equals every field in `paper_state`.

- [ ] **Step 6: Implement full reconstruction and audit**

Audit config hash, ACCOUNT_CREATED payload, sequence continuity, event hash chain, last event hash, cash, shares, pending order, equity peak, drawdown, and last target position. Read-only mode must not call schema creation.

- [ ] **Step 7: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_store.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/paper_store.py tests/test_paper_store.py
git commit -m "feat(paper): persist auditable event ledger"
```

### Task 5: Structured update summary and paper data gate

**Files:**
- Modify: `src/finance_lab/data_manifest.py`
- Create: `src/finance_lab/paper_pipeline.py`
- Create: `tests/paper_helpers.py`
- Modify: `tests/test_data_manifest.py`
- Create: `tests/test_paper_pipeline.py`

**Interfaces:**
- Produces: `read_update_summary(paths: ProjectPaths) -> UpdateSummary`
- Produces: `load_paper_market_snapshot(paths: ProjectPaths, as_of_date: date, stale_after_business_days: int) -> PaperMarketSnapshot`

- [ ] **Step 1: Add update-summary parser tests**

Cover a successful fallback record with positive rows and one source error, missing file, malformed JSON, wrong root type, missing target record, zero rows, and empty curated path. Preserve existing manifest behavior that malformed summaries become an explicit warning instead of crashing.

- [ ] **Step 2: Implement immutable summary types and parser**

Define `UpdateRecord(symbol, rows, curated_file, source_errors)` and `UpdateSummary(end, records, parse_error)`. Refactor the existing private upstream-status loader to consume this parser so old reports retain identical semantics.

- [ ] **Step 3: Add failing gate tests**

Use `tests/paper_helpers.py` to create a temporary sh.510300 ETF, canonical Parquet, instruments config, and update summary. Assert:

- `unadjusted_prices` and `mixed_sources` remain visible but do not block.
- `stale_data`, `large_date_gap`, unknown warnings, errors, zero-row updates, end/as-of mismatch, and all-source failures block without database mutation.
- Replacing the Parquet between manifest and read raises a retry error.

- [ ] **Step 4: Implement the snapshot gate**

The function must require configured kind `etf`, exact symbol `sh.510300`, summary end equal to as-of, positive target rows, nonempty curated path, allowed warning subset, no error codes, staleness within threshold, and matching SHA-256 before and after read. Return prices plus complete `PaperDataContext` including upstream errors.

- [ ] **Step 5: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_data_manifest.py tests/test_paper_pipeline.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/data_manifest.py src/finance_lab/paper_pipeline.py tests/paper_helpers.py tests/test_data_manifest.py tests/test_paper_pipeline.py
git commit -m "feat(paper): enforce market data gate"
```

### Task 6: Portfolio initialization, catch-up processing, and status

**Files:**
- Modify: `src/finance_lab/paper_pipeline.py`
- Modify: `tests/test_paper_pipeline.py`

**Interfaces:**
- Produces: `paper_init_portfolio(portfolio_id: str = "default", ledger_config: LedgerConfig | None = None, as_of_date: date | None = None, stale_after_business_days: int = 3, root: Path | None = None) -> PaperOperationResult`
- Produces: `paper_run_portfolio(portfolio_id: str = "default", as_of_date: date | None = None, stale_after_business_days: int = 3, root: Path | None = None) -> PaperOperationResult`
- Produces: `paper_status_portfolio(portfolio_id: str = "default", root: Path | None = None) -> PaperOperationResult`
- Produces: `PaperPortfolioNotFound` with CLI exit code 4.

- [ ] **Step 1: Write failing initialization tests**

Assert init creates both accounts at the latest bar, records no historical trades or returns, computes the initial signal from history through that bar only, and refuses an existing portfolio without changing rows.

- [ ] **Step 2: Implement locked, atomic initialization**

Acquire `paper_run_lock`, load the gated snapshot, construct the fixed account pair, call `initialize_account` for each, and commit both under one batch ID. Do not fetch data.

- [ ] **Step 3: Write failing daily and catch-up tests**

Append one new bar and assert yesterday's order fills at today's open. Append three bars at once and assert each is processed sequentially with one committed run per account/date. Run again and assert `status == "no-op"` with identical event and fee counts.

- [ ] **Step 4: Implement one-date-at-a-time atomic replay**

For each unseen date, slice history through that date, call `advance_one_bar` for both accounts, and commit both in one transaction. If either account fails, do not commit that date; retain earlier committed dates. Audit before any mutation.

- [ ] **Step 5: Write and implement read-only status tests**

`paper_status_portfolio` opens DuckDB read-only, runs full audit, returns current account snapshots, and does not change event counts, state hashes, or the database modified timestamp.

- [ ] **Step 6: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_pipeline.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/paper_pipeline.py tests/test_paper_pipeline.py
git commit -m "feat(paper): orchestrate forward portfolio runs"
```

### Task 7: Deterministic bilingual-safe reports and audit exports

**Files:**
- Create: `src/finance_lab/paper_report.py`
- Create: `tests/test_paper_report.py`
- Modify: `src/finance_lab/paper_pipeline.py`

**Interfaces:**
- Produces: `write_paper_report(result: PaperOperationResult, paths: ProjectPaths) -> PaperReportPaths`
- Produces deterministic archive stem and `<portfolio>_paper_latest_report.html`.

- [ ] **Step 1: Write failing report tests**

Assert HTML, JSON, daily CSV, trades CSV, and PNG exist; JSON rejects NaN; HTML escapes upstream `<script>` text; all resolved paths remain under outputs; both accounts appear; and the disclaimer contains “模拟盘”“非实盘”“非投资建议”.

- [ ] **Step 2: Implement report view and files**

The archive identity must hash portfolio ID, last committed date, dataset ID, curated SHA-256, account config hashes, and state hashes. Render cash, shares, market value, equity, cumulative return, drawdown, trades, commissions, taxes, slippage, signal, pending order, warnings, and full lineage.

- [ ] **Step 3: Implement deterministic recovery behavior**

When a committed archive is missing, regenerate that exact stem. When it exists, leave it unchanged. Always refresh the latest HTML/JSON pointers from the same view. A no-op must not create a second archive identity.

- [ ] **Step 4: Integrate reports after database commits**

Initialization and runs call the report writer only after the transaction closes. Report failure returns nonzero but never rolls back committed ledger state; a retry follows the no-op recovery path.

- [ ] **Step 5: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_report.py tests/test_paper_pipeline.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/paper_report.py src/finance_lab/paper_pipeline.py tests/test_paper_report.py tests/test_paper_pipeline.py
git commit -m "feat(paper): add daily audit dashboard"
```

### Task 8: CLI commands and Windows one-click workflow

**Files:**
- Modify: `src/finance_lab/cli.py`
- Create: `tests/test_paper_cli.py`
- Create: `scripts/run_paper_trading.ps1`
- Create: `run_paper_trading.cmd`

**Interfaces:**
- Adds commands: `paper-init`, `paper-run`, `paper-status`
- Uses exit code 4 only for a missing portfolio; other errors remain nonzero.

- [ ] **Step 1: Write failing parser and dispatch tests**

Parse every documented argument, monkeypatch each pipeline function, assert the correct arguments and UTF-8 JSON output, and assert missing portfolio returns 4 without a traceback.

- [ ] **Step 2: Add CLI parsers and dispatch**

`paper-init` accepts portfolio, all LedgerConfig values, as-of, and stale threshold. `paper-run` accepts portfolio, as-of, and stale threshold. `paper-status` accepts portfolio. Print the report path plus a JSON summary and preserve the generic exception handler.

- [ ] **Step 3: Write the PowerShell workflow**

`run_paper_trading.ps1` must:

1. Install the environment only when Python is missing and check `LASTEXITCODE`.
2. Run `fetch` with a shared as-of date and stop on failure.
3. Run `manifest` with that as-of date and stop on failure.
4. Run `paper-run`.
5. If and only if exit code is 4, run `paper-init` with defaults.
6. Stop on every other nonzero code.
7. Run `paper-status` and open the latest HTML only after success.

- [ ] **Step 4: Add script contract tests**

Read both scripts and assert command order, every native-call exit check, UTF-8 setup, and the exact latest report path. On Windows, parse the PowerShell file with `System.Management.Automation.Language.Parser` and require zero syntax errors.

- [ ] **Step 5: Verify and commit**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_paper_cli.py -v`
Expected: PASS.

```powershell
git add src/finance_lab/cli.py tests/test_paper_cli.py scripts/run_paper_trading.ps1 run_paper_trading.cmd
git commit -m "feat(paper): add beginner daily commands"
```

### Task 9: Documentation, version, experiment protocol, and package

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `pyproject.toml`, `src/finance_lab/__init__.py`
- Create: `docs/第九课.md`, `experiments/008_forward_paper_trading.md`, `docs/releases/v0.9.0.md`
- Modify: `scripts/package_release.ps1`

- [ ] **Step 1: Update public documentation**

README adds lesson 9, the three commands, one-click script, scope limits, and a paper-trading roadmap check. The lesson explains first initialization, daily use, pending orders, data failures, and recovery in beginner language.

- [ ] **Step 2: Record the forward protocol honestly**

`experiments/008_forward_paper_trading.md` fixes both strategies and all costs, states that forward observation has not started in the source release, and says the actual local start date is recorded by the first ACCOUNT_CREATED event. It contains no invented result.

- [ ] **Step 3: Bump and document v0.9.0**

Set both package version locations to `0.9.0`, add changelog and release notes, and state that source archives exclude market data, DuckDB accounts, and user outputs.

- [ ] **Step 4: Extend package verification**

Set the packaging default to `v0.9.0` and add every new source, test, script, lesson, experiment, and release note to `RequiredArchivePaths`. Preserve compileall, CLI help, and full pytest extraction checks.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src tests
```

Expected: all commands exit 0.

```powershell
git add README.md CHANGELOG.md pyproject.toml src/finance_lab/__init__.py docs experiments scripts/package_release.ps1
git commit -m "docs: publish v0.9 paper trading workflow"
```

### Task 10: Fresh verification, release archive, and review gate

**Files:**
- Verify all changed files.
- Create no persistent paper account in the repository.

- [ ] **Step 1: Run the complete local quality gate**

```powershell
.\scripts\verify.ps1
git diff --check main...HEAD
git status --short
```

Expected: pytest, Ruff, and mypy pass; diff check is clean; only intentional branch changes are present.

- [ ] **Step 2: Verify the source package**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package_release.ps1 -Version v0.9.0`
Expected: archive extraction tests pass and the script prints file count plus SHA-256.

- [ ] **Step 3: Perform targeted safety review**

Inspect for future leakage, duplicate-run paths, transaction boundaries, SQL interpolation, output path escape, NaN JSON, unreported source failures, real-trading language, and accidental inclusion of `data/finance_lab.duckdb` or market data.

- [ ] **Step 4: Request independent code review**

Use the requesting-code-review skill against `main...HEAD`. Fix each verified P0/P1/P2 issue with a new failing regression test, rerun targeted tests, and commit the fix with a scoped message.

- [ ] **Step 5: Run final verification after review fixes**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src tests
git diff --check main...HEAD
git status --short --branch
```

Expected: all commands exit 0 and the worktree is clean.

- [ ] **Step 6: Prepare PR; do not merge without user confirmation**

The PR summary must state the forward-only boundary, immutable event ledger, data gate, hypothetical costs, no broker connection, test counts, package SHA-256, and remaining limitations.
