# How to Use VAST - Real Examples

VAST is now connected to your Pagila movie database. Here's how to have a conversation with it:

## Starting VAST

```bash
python start_vast.py
```

Then just type naturally!

## Real Conversations You Can Have

### 1. Analytics Questions

**You**: "How many customers do we have?"

**VAST**: 
- Generates SQL: `SELECT COUNT(*) FROM customer`
- Executes it
- Returns the actual count from your database

**You**: "Show me the top 5 customers by number of rentals"

**VAST**:
- Writes a complex JOIN query
- Groups by customer
- Orders by rental count
- Shows you the actual top 5 customers

### 2. Database Modifications

**You**: "I want to add a customer review system for films"

**VAST**:
- Designs the schema (review table with customer_id, film_id, rating, comment)
- Shows you the CREATE TABLE statement
- Asks for confirmation
- Actually creates the table in your database
- Remembers this for future conversations

### 3. Business Rules

**You**: "From now on, never delete customer data - always use soft deletes"

**VAST**:
- Adds this as a business rule
- Will enforce it in all future operations
- Remembers it permanently

### 4. Complex Analysis

**You**: "Which films have never been rented?"

**VAST**:
- Writes a LEFT JOIN query
- Finds films with no rental records
- Executes and shows results

## What Makes VAST Different

| Action | ChatGPT | VAST |
|--------|---------|------|
| "How many films do we have?" | Generates SQL, you copy/paste | **Executes directly, shows result** |
| "Add a review system" | Suggests schema, you implement | **Creates tables immediately** |
| "Remember this rule..." | Forgets after session | **Remembers forever** |
| Next conversation | Starts from zero | **Continues where you left off** |

## The Key Point

With VAST, you're not just getting SQL suggestions - you're having a conversation with an AI that:

1. **Actually runs queries** on your real database
2. **Makes schema changes** when you ask
3. **Remembers everything** between sessions
4. **Enforces your rules** consistently

## Try It Now

```bash
# Start VAST
python start_vast.py

# Then try these:
"What's the total revenue from rentals?"
"Show me which categories are most popular"
"Add a wishlist feature for customers"
"Which store has the most inventory?"
```

Each query actually runs. Each change actually happens. Everything is remembered.

That's the difference - VAST isn't just an AI that knows about databases. It's an AI that **operates** your database.
