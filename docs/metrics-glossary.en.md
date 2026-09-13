# Backtest metrics glossary

Use this reference when reading an A-Share Quant Lab backtest report. The definitions below describe what this project calculates; another tool may use a different annualization rule, risk-free rate, benchmark, or cost model.

All examples are synthetic. They explain arithmetic, not expected performance or an investment opportunity. Compare two results only when their evaluated dates, data, strategy parameters, and cost assumptions are the same.

The exact implementation is in [`backtest.py`](../src/finance_lab/backtest.py).

## Total return

**Definition.** The percentage change from starting equity to final equity after the configured turnover costs. The project starts equity at `1.0` and calculates `final equity - 1`.

**Read it as.** A total return of `0.10`, displayed as `10.00%`, means one synthetic unit finished at `1.10` over the complete evaluated interval.

**Caveat.** Total return says nothing about how long the interval lasted or how severe the losses were along the way. Do not compare total returns from different date ranges without examining the other metrics.

## Annualized return

**Definition.** The compounded total return scaled to 252 trading observations: `final equity ** (252 / observations) - 1`.

**Read it as.** If synthetic equity rises from `1.0` to `1.10` over exactly 252 evaluated observations, both total return and annualized return are `10%`.

**Caveat.** Annualization does not make a short or unrepresentative sample reliable. A few unusually good or bad observations can become an extreme annualized number.

## Annualized volatility

**Definition.** The population standard deviation of evaluated daily returns (`ddof=0`) multiplied by the square root of 252.

**Read it as.** If the daily standard deviation is `1%`, the reported annualized volatility is approximately `15.87%` because `1% × sqrt(252) ≈ 15.87%`.

**Caveat.** Volatility measures dispersion, not the probability of permanent loss. The 252-day scaling convention also assumes daily observations are comparable through time.

## Sharpe ratio

**Definition.** Mean daily return divided by the population standard deviation of daily returns, then multiplied by the square root of 252. This implementation assumes a zero risk-free rate; it returns `0.0` when volatility is zero or there are fewer than two observations.

**Read it as.** A larger value means more historical average return per unit of measured daily variability under this exact sample and convention.

**Caveat.** Sharpe is not a probability of success and is sensitive to the sample, outliers, serial dependence, and the zero-risk-free-rate assumption. Never use it alone to rank strategies.

## Maximum drawdown

**Definition.** The deepest percentage fall from any prior equity peak to a later trough. The calculation includes the initial equity of `1.0`, so an immediate loss is not hidden.

**Read it as.** Equity falling from a synthetic peak of `1.10` to `0.99` has a `-10%` drawdown. A more negative number represents a deeper historical decline.

**Caveat.** Maximum drawdown records only the worst depth in the observed interval. It does not show how long the decline lasted, how often drawdowns occurred, or what may happen later.

## Trade sides

**Definition.** The rounded sum of absolute position changes (`turnover`) in the basic backtest. Moving from no position (`0`) to fully invested (`1`) is one side; returning from `1` to `0` is another side.

**Read it as.** Entering once and later exiting once produces two trade sides, which is one completed round trip.

**Caveat.** Trade sides are not the same as order count, fill count, or round-trip count. A position still open at the end has an entry side without a matching exit, and execution-feasibility reports may apply additional order rules.

## Exposure

**Definition.** The arithmetic mean of the strategy position over evaluated observations. In the basic long-or-cash strategies, position is normally `1` when invested and `0` when in cash.

**Read it as.** Positions `[0, 1, 1]` produce exposure of `2 / 3`, displayed as `66.67%`.

**Caveat.** Exposure measures time in the modelled position, not the position's risk, concentration, liquidity, or capital loss potential.

## Benchmark

**Definition.** A comparison rule evaluated on the same return interval. The basic report uses a buy-and-hold price benchmark on the same forward-open price path and applies the same configured one-sided cost when the benchmark enters.

**Read it as.** If a strategy returns `6%` and the benchmark returns `8%`, the strategy lagged by `2` percentage points. That difference is not automatically alpha.

**Caveat.** Conclusions depend on benchmark choice and data adjustment. A price benchmark built from unadjusted prices may exclude dividends or other company actions, so it must not be described as a complete total-return benchmark.

## Reproducible synthetic example

Start with equity `1.0` and apply three synthetic daily returns in order: `+10%`, `-5%`, and `+2%`.

```text
1.0 × 1.10 × 0.95 × 1.02 = 1.0659
total return = 1.0659 - 1.0 = 6.59%
maximum drawdown = (1.045 / 1.10) - 1 = -5.00%
```

The example is deliberately too short for a meaningful annualized return, annualized volatility, or Sharpe ratio. Its purpose is only to make compounding and drawdown arithmetic reproducible.

## Reading order

Read total return together with maximum drawdown, volatility, exposure, and trade sides. Then compare the strategy with its benchmark under identical dates and assumptions. Finally, check whether the result is from development data, a fixed out-of-sample interval, a walk-forward study, or a forward observation; the metric name alone does not establish evidence quality.
