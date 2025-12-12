# ruff: noqa: D212, D415
from typing import Annotated
import sys
from contextlib import contextmanager
import importlib

from loguru import logger
from rich import print as rprint
from rich.console import Console
from rich.table import Table
from typer import Argument, Context, Option, Typer
from typer import run as typer_run

from . import utils

cli = Typer(rich_markup_mode="rich")
cli.add_typer(utils.app, name="utils", help="[bold cyan]Utility commands[/]")


@contextmanager
def _patched_argv(argv: list[str]):
    prev = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = prev


@cli.command("print-ynab")
def print_ynab_transactions(
    api_key: Annotated[
        str | None,
        Argument(
            help="YNAB API key",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.ynab_api_key.get_secret_value(),
        ),
    ],
    budget_id: Annotated[
        str | None,
        Argument(
            help="YNAB Budget ID",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.ynab_budget_id.get_secret_value(),
        ),
    ],
) -> None:
    """
    [bold cyan]Prints YNAB transactions.[/]

    [yellow i]All arguments will use defaults in .env file if not provided.[/]
    """
    console = Console()

    from ynab.configuration import Configuration
    from ynamazon.ynab_transactions import get_ynab_transactions

    configuration = Configuration(access_token=api_key)
    transactions, _payee = get_ynab_transactions(configuration=configuration, budget_id=budget_id)

    console.print(f"[bold green]Found {len(transactions)} transactions.[/]")

    if not transactions:
        console.print("[bold red]No transactions found.[/]")
        exit(1)

    table = Table(title="YNAB Transactions")
    table.add_column("Date", justify="left", style="cyan", no_wrap=True)
    table.add_column("Amount", justify="right", style="green")
    table.add_column("Memo", justify="left", style="yellow")

    for transaction in transactions:
        table.add_row(
            str(transaction.var_date),
            f"${-transaction.amount_decimal:.2f}",
            transaction.memo or "n/a",
        )

    console.print(table)


@cli.command("print-amazon")
def print_amazon_transactions(
    user_email: Annotated[
        str,
        Argument(
            help="Amazon username",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.amazon_user,
        ),
    ],
    user_password: Annotated[
        str,
        Argument(
            help="Amazon password",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.amazon_password.get_secret_value(),
        ),
    ],
    order_years: Annotated[
        list[int] | None,
        Option("-y", "--years", help="Order years; leave empty for current year"),
    ] = None,
    transaction_days: Annotated[
        int, Option("-d", "--days", help="Days of transactions to retrieve")
    ] = 31,
) -> None:
    """
    [bold cyan]Prints Amazon transactions.[/]

    [yellow i]All required arguments will use defaults in .env file if not provided.[/]
    """
    console = Console()

    from ynamazon.amazon_transactions import AmazonConfig, get_amazon_transactions

    config = AmazonConfig(username=user_email, password=user_password)  # pyright: ignore[reportArgumentType]

    transactions = get_amazon_transactions(
        configuration=config,
        order_years=order_years,
        transaction_days=transaction_days,
    )

    console.print(f"[bold green]Found {len(transactions)} transactions.[/]")

    if not transactions:
        console.print("[bold red]No transactions found.[/]")
        exit(1)

    table = Table(title="Amazon Transactions")
    table.add_column("Completed Date", justify="left", style="cyan", no_wrap=True)
    table.add_column("Transaction Total", justify="right", style="green")
    table.add_column("Order Total", justify="right", style="green")
    table.add_column("Order Number", justify="center", style="cyan")
    table.add_column("Order Link", justify="center", style="blue underline")
    table.add_column("Item Names", justify="left", style="yellow")

    for transaction in transactions:
        table.add_row(
            str(transaction.completed_date),
            f"${transaction.transaction_total:.2f}",
            f"${transaction.order_total:.2f}",
            transaction.order_number,
            str(transaction.order_link),
            " | ".join(item.title for item in transaction.items),
        )

    console.print(table)


@cli.command()
def ynamazon(
    ynab_api_key: Annotated[
        str | None,
        Argument(
            help="YNAB API key",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.ynab_api_key.get_secret_value(),
        ),
    ],
    ynab_budget_id: Annotated[
        str | None,
        Argument(
            help="YNAB Budget ID",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.ynab_budget_id.get_secret_value(),
        ),
    ],
    amazon_user: Annotated[
        str,
        Argument(
            help="Amazon username",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.amazon_user,
        ),
    ],
    amazon_password: Annotated[
        str,
        Argument(
            help="Amazon password",
            default_factory=lambda: __import__(
                "ynamazon.settings", fromlist=["settings"]
            ).settings.amazon_password.get_secret_value(),
        ),
    ],
    force_logout: Annotated[
        bool, Option("--force-logout", "-f", help="Force logout of Amazon account")
    ] = False,
    debug: Annotated[bool, Option("--debug", "-d", help="Enable debug mode")] = False,
) -> None:
    """
    [bold cyan]Match YNAB transactions to Amazon Transactions and optionally update YNAB Memos.[/]

    [yellow i]All required arguments will use defaults in .env file if not provided.[/]
    """
    console = Console()
    if debug:
        logger.debug("Debug mode enabled. Logging set to DEBUG level.")
        logger.debug(f"Amazon Credentials: {amazon_user}")

    from ynab.configuration import Configuration
    from ynamazon.amazon_transactions import AmazonConfig
    from ynamazon.main import process_transactions

    config = AmazonConfig(username=amazon_user, password=amazon_password, debug=debug)  # pyright: ignore[reportArgumentType]

    if force_logout:
        console.print("[bold yellow]Forcing logout of Amazon account...[/]")
        config.amazon_session().logout()

    process_transactions(
        amazon_config=config,
        ynab_config=Configuration(access_token=ynab_api_key),
        budget_id=ynab_budget_id,
    )


@cli.command("create-missing")
def create_missing_ynamazon(
    history_pages: Annotated[
        int | None,
        Option(
            "-p",
            "--history-pages",
            help="Also scan N pages per year from order history (bridges to create_missing_ynamazon.py)",
        ),
    ] = None,
    history_page_size: Annotated[
        int | None,
        Option(
            "--history-page-size",
            help="Override assumed page size (default 10) for startIndex math (bridges to create_missing_ynamazon.py)",
        ),
    ] = None,
) -> None:
    """[bold cyan]Create or update itemized Amazon orders in YNAB.[/]"""
    try:
        mod = importlib.import_module("create_missing_ynamazon")
    except Exception as e:
        rprint(
            "[bold red]Could not import create_missing_ynamazon.py.[/] "
            "Run this from the repository root (or ensure it is on PYTHONPATH)."
        )
        raise e

    script_argv: list[str] = ["create_missing_ynamazon.py"]
    if history_pages is not None:
        script_argv.extend(["-p", str(history_pages)])
    if history_page_size is not None:
        script_argv.extend(["--history-page-size", str(history_page_size)])

    with _patched_argv(script_argv):
        main = getattr(mod, "main", None)
        if not callable(main):
            raise RuntimeError("create_missing_ynamazon.py does not expose a callable main()")
        main()


@cli.callback(invoke_without_command=True)
def yna_callback(ctx: Context) -> None:
    """
    [bold cyan]Run 'yna' to match and update transactions using the arguements in .env. [/]

    [yellow i]Use 'yna ynamazon [ARGS]' to use command-line arguements to override .env. [/]
    """
    rprint("[bold cyan]Starting YNAmazon processing...[/]")
    if ctx.invoked_subcommand is None:
        typer_run(function=ynamazon)


if __name__ == "__main__":
    cli()
