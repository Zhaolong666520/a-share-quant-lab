# Quick start: generate your first offline report

This tutorial is for first-time users. Its goal is to generate and open one A-Share Quant Lab report in about 10 minutes while confirming that Python and the project entrypoints work on your computer.

The demo uses deterministic synthetic data. It does not download market prices, connect to a broker, read an account, recommend a security, or place an order. The first dependency installation still needs access to a Python package index; generating the report does not contact a market-data source.

[简体中文教程](快速开始.md)

## What you will create

After a successful run, the `outputs/` directory contains:

| File | Purpose |
| --- | --- |
| `demo_000300_sma_report.html` | Human-readable report for a web browser |
| `demo_000300_sma_equity.png` | Strategy and buy-and-hold equity curves |
| `demo_000300_sma_metrics.json` | Complete machine-readable metrics |

The current demo contains 899 deterministic synthetic observations. The 20/60-day moving-average strategy returns about 41.96%, while its synthetic buy-and-hold benchmark returns about 91.33%. These numbers only demonstrate reproducibility. Performance on synthetic data has no investment meaning.

## Prerequisites

Install Python 3.11, 3.12, or 3.13. Python 3.12 is recommended.

You can clone the repository with Git or download the source ZIP from GitHub Releases. Open a terminal in the project root after cloning or extracting it.

## Windows

1. Double-click `run_demo.cmd` in the project root.
2. On the first run, wait while the script creates `.venv` and installs the locked dependencies.
3. Open `outputs\demo_000300_sma_report.html`.

You can also run the launcher from PowerShell:

```powershell
.\run_demo.cmd
```

## Linux or macOS

Run the portable shell launcher from the project root:

```sh
sh run_demo.sh
```

If you cloned the repository and executable permissions were preserved, this also works:

```sh
./run_demo.sh
```

Open the report after the command finishes:

```sh
# macOS
open outputs/demo_000300_sma_report.html

# Linux desktop
xdg-open outputs/demo_000300_sma_report.html
```

On a server without a desktop environment, download the HTML and PNG files and open them locally.

## Verify the result

The report must display a synthetic-data warning near the top. Its data source must be `SYNTHETIC_DEMO_NOT_MARKET_DATA`.

The JSON strategy metrics should remain identical for the same project version. PNG hashes can differ across operating systems because Matplotlib and fonts may render the same chart differently.

Stop and open an issue if the report describes the demo as real market data, does not show the warning, or produces inconsistent JSON metrics on the same version.

## Troubleshooting

### Python 3.11-3.13 was not found

Install Python 3.12, open a new terminal, and run the launcher again. If an unsupported Python version created the existing `.venv`, remove only the `.venv` directory and retry. Do not remove `data/`, `experiments/`, or other project directories.

### Permission denied on Linux or macOS

Use the launcher without relying on its executable bit:

```sh
sh run_demo.sh
```

### Dependency installation failed

The first run downloads Python packages. Check your network and package-index access, then run the same command again. The launcher reuses an existing `.venv`.

### The report did not open automatically

The launcher prints the report path but does not force a browser to open. Open `outputs/demo_000300_sma_report.html` manually.

## Next step

Read [`第一课`](第一课.md) to understand total return, maximum drawdown, and benchmarks. The lesson is currently in Chinese. Before using real data, also read the [`data notes`](数据说明.md) and preserve the project's provenance, fingerprint, freshness, and no-look-ahead safeguards.
