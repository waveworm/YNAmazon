# YNAmazon
A program to annotate YNAB transactions with Amazon order info, supporting both gift card and credit card purchases with itemized splits.

## Features
- **Payment Transactions Mode**: Uses Amazon's payment transactions page to accurately track both gift card and credit card purchases
- **Split Payment Support**: Orders paid with multiple payment methods (e.g., gift card + credit card) are automatically split into separate YNAB transactions
- **Itemized Splits**: Multi-item orders create split transactions in YNAB with each item on its own line
- **Smart Routing**: Transactions are automatically routed to the correct YNAB account based on payment method
- **Debug Mode**: Test mode that creates transactions with blue flags for easy identification and cleanup

## Setup/Prerequisites
1. **YNAB and Amazon Accounts**: Ensure you have active accounts for both YNAB and Amazon.
2. **Create a Renaming Rule in YNAB**:
   - In YNAB, go to the "Manage Payees" menu.
   - Create a rule to automatically rename any transactions containing "Amazon" to the payee name you want to use to indicate that the transaction needs to be processed. The default is `"Amazon - Needs Memo"`.
3. **Create a Processed Payee in YNAB**:
   - Create a payee in YNAB to indicate that a transaction has already been processed. For example, `"Amazon"`.
4. **Install Toolkit for YNAB (Optional)**:
   - Install the [Toolkit for YNAB](https://toolkitforynab.com/) browser extension.
   - Enable the following features for the best experience:
     - "Enable Markdown in Memos"
     - "Hyperlinks in the memo field"
5. **Set Up Environment Variables**:
   - Create a `.env` file to securely store your credentials and configuration:
     1. Make a copy of `.env-template` and rename it to `.env`.
     2. Add the following variables to the `.env` file:
        ```plaintext
        YNAB_API_KEY=your-ynab-api-key
        YNAB_BUDGET_ID=your-budget-id
        YNAB_TARGET_ACCOUNT_ID=your-default-account-id
        YNAB_PAYEE_NAME_TO_BE_PROCESSED="Amazon - Needs Memo"
        YNAB_PAYEE_NAME_PROCESSING_COMPLETED=Amazon
        YNAB_USE_MARKDOWN=true/false
        YNAB_USE_AI_SUMMARIZATION=true/false
        OPENAI_API_KEY=your-openai-api-key
        AMAZON_USER=your-amazon-email
        AMAZON_PASSWORD=your-amazon-password
        ```
6. **Install Dependencies**:
   - Install `uv` ([instructions](https://github.com/astral-sh/uv?tab=readme-ov-file#installation)) if needed
   - Run one of the following commands to install the required dependencies:
     ```bash
     # Basic installation (without AI features)
     uv sync
     
     # Installation with AI features
     uv sync --extra ai
     ```

## Use CLI

[CLI Instructions](/CLI_README.md)

## How it works
This program automates the process of annotating YNAB transactions with detailed Amazon order information. Here's how it works:

1. **Amazon Transactions Retrieval**: The program logs into your Amazon account using the `amazon-orders` library and retrieves your recent order history and transactions. It matches transactions with corresponding orders based on order numbers.

2. **YNAB Transactions Retrieval**: It connects to your YNAB account via the YNAB API and identifies transactions with a specific payee name (e.g., "Amazon - Needs Memo") that require annotation.

3. **Transaction Matching**: The program compares YNAB transactions with Amazon transactions by matching amounts to find corresponding orders.

4. **Memo Annotation**: For each matched transaction, it generates a detailed memo that includes:
   - A note if the transaction does not represent the full order total.
   - A list of items in the order.
   - A link to the Amazon order details.
   - If AI summarization is enabled, it will use OpenAI to generate a concise summary of the order.

5. **Transaction Update**: The program updates the YNAB transaction with the generated memo and changes its payee to a designated name (e.g., "Amazon") to mark it as processed.

6. **Automation**: This process is fully automated, reducing manual effort and ensuring accurate reconciliation of Amazon purchases in your YNAB budget.

The script relies on the `amazon-orders` library for Amazon data and the YNAB API for transaction updates.

There is an important distinction between an Amazon order and an Amazon transaction. When you check out, that is a single order. Often, one order will be fulfilled together, creating a single transaction. However, some orders will generate more than one transaction, which will show up in YNAB separately. This program handles this by adding the same memo to each transaction of that order, which includes a note that the transaction doesn't reflect the entire order.

### Sample Memo Format

For a transaction for an order with a single item, the memo format is as minimal as possible, for example:
``` plaintext
60 Gallon Barrel of Maple Syrup
https://www.amazon.com/gp/css/summary/edit.html?orderID=123-1234567-7654321
```

For orders with more than one item, they are listed in a numbered markdown list:
``` plaintext
1. Dog Costume
2. Facepaint
3. Popcorn-shaped Purse
https://www.amazon.com/gp/css/summary/edit.html?orderID=321-7890123-4567890
```

If AI summarization is enabled, the memo will be a concise summary of the order:
``` plaintext
Dog Costume, Facepaint, and Popcorn-shaped Purse
https://www.amazon.com/gp/css/summary/edit.html?orderID=321-7890123-4567890
```

If Amazon splits the order into multiple transactions, this program will detect that and warn you in the memo. In this case, YNAB will label all transactions for that order with the same memo. Unfortunately, it is not possible to easily tell which items belong to what transactions, so you may have to click on the link and figure it out for yourself:
``` plaintext
-This transaction doesn't represent the entire order. The order total is $99.99-

1. Dog Costume
2. Facepaint
3. Popcorn-shaped Purse
https://www.amazon.com/gp/css/summary/edit.html?orderID=321-7890123-4567890
```

## AI Summarization
The program can use OpenAI's GPT model to generate concise summaries of your Amazon orders. To enable this feature:

1. Install the package with AI features:
   ```bash
   uv sync --extra ai
   ```
2. Set `YNAB_USE_AI_SUMMARIZATION=true` in your `.env` file
3. Add your OpenAI API key: `OPENAI_API_KEY=your-openai-api-key`

When enabled, the program will:
- Generate concise summaries of orders with multiple items
- Preserve important information like order URLs and partial order warnings
- Fall back to standard truncation if AI summarization fails or is unavailable
- Will not use markdown formatting if enabled since this only increases memo length

## Payment Transactions Mode (Recommended)

The recommended way to use YNAmazon is with Payment Transactions Mode, which scrapes Amazon's payment transactions page (`https://www.amazon.com/cpe/yourpayments/transactions`) to get accurate payment method and amount information for each order.

### Why Use Payment Transactions Mode?
- **Accurate Payment Tracking**: Shows exactly which payment method was used for each order
- **Split Payment Support**: If an order was paid with both gift card and credit card, it creates separate transactions for each
- **Credit Card Support**: Tracks credit card purchases in addition to gift card purchases

### Configuration

Add these environment variables to your `.env` file:

```plaintext
# Enable payment transactions mode
AMAZON_USE_PAYMENT_TRANSACTIONS=true

# Account routing (get account IDs from YNAB URL when viewing the account)
YNAB_GIFT_CARD_ACCOUNT_ID=your-gift-card-account-id
YNAB_DEFAULT_CREDIT_CARD_ACCOUNT_ID=your-credit-card-account-id

# Optional: Map specific credit cards to different accounts
# Format: JSON object mapping last 4 digits to account IDs
YNAB_CREDIT_CARD_ACCOUNTS={"1234": "account-id-for-card-ending-1234", "5678": "account-id-for-card-ending-5678"}
```

### How It Works
1. Fetches the payment transactions page from Amazon
2. Extracts payment method, amount, and order ID for each transaction
3. For each unique order, fetches the order details to get itemized product information
4. Creates YNAB transactions with:
   - **Gift card payments** → Routed to gift card account, marked as "cleared"
   - **Credit card payments** → Routed to credit card account, marked as "uncleared" (so you can match with bank import)
   - **Split payments** → Creates separate transactions for each payment method with proportionally scaled item amounts

### Transaction Format
- **Single-item orders**: Created as regular transactions (no split)
- **Multi-item orders**: Created as split transactions with each item on its own line, including the item name and price in the memo

## Transaction Flags

The script uses YNAB flag colors to help you identify transactions:

- **🟢 Green Flag**: New transactions created in normal mode
- **🔵 Blue Flag**: Transactions created in debug mode (for testing)

All transactions are created **uncategorized** so you can assign the appropriate category in YNAB.

## Debug Mode

Debug mode helps you test the script without cluttering your YNAB with permanent transactions.

### Enable Debug Mode
```plaintext
YNAMAZON_DEBUG=true
```

### What Debug Mode Does
- **Blue Flag**: All created transactions have a blue flag for easy identification
- **Bypasses Duplicate Check**: Always creates fresh transactions (ignores existing ones)
- **Unique Import IDs**: Uses a timestamp-based import ID tag so transactions are always created fresh
- **10-Day Lookback**: Forces a 10-day lookback period regardless of your normal setting

### Normal Mode (Production)
When `YNAMAZON_DEBUG=false`:
- **Green Flag**: New transactions have a green flag
- **Duplicate Prevention**: Skips orders that already exist in YNAB (checks last 90 days)
- **Stable Import IDs**: Uses consistent import IDs so the same order won't be created twice

### Cleanup
1. In YNAB, filter transactions by blue flag
2. Select all and delete
3. Set `YNAMAZON_DEBUG=false` when ready for production use

## All Environment Variables

### Required
| Variable | Description |
|----------|-------------|
| `YNAB_API_KEY` | Your YNAB Personal Access Token |
| `YNAB_BUDGET_ID` | Your YNAB budget ID (from URL) |
| `YNAB_TARGET_ACCOUNT_ID` | Default account for transactions |
| `AMAZON_USERNAME` | Your Amazon email |
| `AMAZON_PASSWORD` | Your Amazon password |

### Account Routing
| Variable | Description |
|----------|-------------|
| `YNAB_GIFT_CARD_ACCOUNT_ID` | Account for gift card purchases |
| `YNAB_DEFAULT_CREDIT_CARD_ACCOUNT_ID` | Default account for credit card purchases |
| `YNAB_CREDIT_CARD_ACCOUNTS` | JSON mapping card last-4-digits to account IDs |

### Amazon Data Sources
| Variable | Description |
|----------|-------------|
| `AMAZON_USE_PAYMENT_TRANSACTIONS` | Use payment transactions page (recommended) |
| `AMAZON_GC_ACTIVITY` | Use gift card activity pages |
| `AMAZON_GC_PLAYWRIGHT` | Use Playwright for interactive scraping |
| `AMAZON_ORDER_REPORT_CSV` | Path to Amazon order report CSV |

### Behavior
| Variable | Description |
|----------|-------------|
| `YNAB_AMAZON_LOOKBACK_DAYS` | Days to look back for orders (default: 45) |
| `YNAB_UPDATE_EXISTING` | Update existing transactions by import_id |
| `YNAB_IMPORT_ID_TAG` | Suffix for import_id to bypass duplicate detection |
| `YNAMAZON_DEBUG` | Enable debug mode with blue flags |

### Debug/Development
| Variable | Description |
|----------|-------------|
| `AMZN_DEBUG` | Enable HTML debug snapshots |
| `AMAZON_PARSER_DEBUG` | Write per-order parse debug files |
| `AMAZON_DUMP_DIR` | Directory for debug artifacts (default: `.amazon_debug`) |

## Limitations
This script probably won't be able to handle weird edge cases. The amazon-orders library is only able to handle amazon.com and will not pull data from other countries' amazon sites. Any transactions in the amazon transaction history that don't relate to an amazon.com order will be ignored. As with any tool that relies on web scraping, things can change at any time and it is up to the maintainers of the amazon-orders library to fix things.

## Disclaimer
This script requires your Amazon and YNAB credentials. Use at your own risk and ensure you store your credentials securely. The author is not responsible for any misuse or data breaches.
