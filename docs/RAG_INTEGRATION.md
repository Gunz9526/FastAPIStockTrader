# RAG Service Integration Guide

## Overview
Trading Bot now provides comprehensive API endpoints for external RAG (Retrieval-Augmented Generation) services to access trading data, portfolio information, and strategy details.

## Architecture

```
RAG Service (External)
       ↓
   HTTP API (/rag/*)
       ↓
Trading Bot (Port 8000)
       ↓
PostgreSQL (READ ONLY for RAG)
```

## Database Access

### Option 1: Direct Database (Recommended)
```bash
# Create READ ONLY user
docker-compose exec db psql -U postgres -d stocktrader -f /app/scripts/create_rag_user.sql

# Connection string for RAG service
DATABASE_URL=postgresql://rag_reader:PASSWORD@db:5432/stocktrader
```

### Option 2: REST API
All endpoints require `X-API-Key` header.

```bash
curl -H "X-API-Key: your-key" http://localhost:8000/rag/positions
```

## Available Endpoints

### 1. OHLCV Data
**Endpoint**: `GET /rag/ohlcv/{symbol}?days=7`

**Purpose**: Get price history for trend analysis

**Response**:
```json
{
  "symbol": "AAPL",
  "period_days": 7,
  "bars_count": 7,
  "data": [...],
  "summary": {
    "latest_price": 150.25,
    "highest": 152.50,
    "lowest": 148.00
  }
}
```

**RAG Usage**: "What's the recent price trend for AAPL?"

### 2. Fundamentals
**Endpoint**: `GET /rag/fundamentals/{symbol}`

**Purpose**: Get valuation metrics (PER, PBR, ROE)

**Response**:
```json
{
  "symbol": "AAPL",
  "per": 28.5,
  "pbr": 35.2,
  "roe": 0.45,
  "market_cap": 3000000000000,
  "sector": "Technology"
}
```

**RAG Usage**: "Is AAPL undervalued?"

### 3. Portfolio
**Endpoint**: `GET /rag/portfolio/{user_id}`

**Purpose**: Get user holdings with P&L

**Response**:
```json
{
  "user_id": "user123",
  "total_value": 50000,
  "total_unrealized_pl": 2500,
  "holdings": [
    {
      "symbol": "AAPL",
      "quantity": 100,
      "avg_price": 145.00,
      "current_price": 150.00,
      "unrealized_pl": 500,
      "pl_percentage": 3.45
    }
  ]
}
```

**RAG Usage**: "How much have I made on AAPL?"

### 4. Trade Decisions (Logs)
**Endpoint**: `GET /rag/trade-decisions/{symbol}?days=7`

**Purpose**: Understand why bot traded

**Response**:
```json
{
  "symbol": "AAPL",
  "decisions": [
    {
      "timestamp": "2025-12-29T10:30:00Z",
      "action": "BUY",
      "reason": "Momentum BUY: Golden=true, MACD=true, ADX=32.5",
      "metrics": {
        "prediction": 0.85,
        "price": 150.00,
        "quantity": 10
      }
    }
  ]
}
```

**RAG Usage**: "Why did you buy AAPL yesterday?"

### 5. Current Positions
**Endpoint**: `GET /rag/positions?status=OPEN`

**Purpose**: Get all open/closed positions

**Response**:
```json
{
  "positions_count": 3,
  "positions": [
    {
      "symbol": "AAPL",
      "status": "OPEN",
      "entry_price": 145.00,
      "current_price": 150.00,
      "unrealized_pl": 500,
      "stop_loss": 142.00,
      "take_profit": 155.00
    }
  ]
}
```

**RAG Usage**: "What positions are currently open?"

### 6. Strategies Info
**Endpoint**: `GET /rag/strategies`

**Purpose**: Explain trading strategies

**Response**:
```json
{
  "strategies": [
    {
      "name": "Momentum",
      "description": "Follows trends using SMA crossovers",
      "best_for": "Strong trending markets"
    }
  ],
  "voting_system": {
    "consensus_required": "50% agreement"
  }
}
```

**RAG Usage**: "What trading strategies are you using?"

### 7. Trade History
**Endpoint**: `GET /rag/trade-history?symbol=AAPL&days=30`

**Purpose**: Audit trail of all trades

**Response**:
```json
{
  "trades_count": 15,
  "total_realized_pl": 1500,
  "trades": [...]
}
```

**RAG Usage**: "Show me all trades in the last month"

## Security

### API Key Authentication
All endpoints require:
```
X-API-Key: your-secret-key
```

Set in `.env`:
```
API_SECRET_KEY=your-secret-key-here
```

### Database Permissions
`rag_reader` user has **READ ONLY** access to:
- ✅ stock_ohlcv
- ✅ stock_fundamentals  
- ✅ portfolio_status
- ✅ positions
- ✅ trade_logs
- ❌ NO WRITE permissions

## Example RAG Integration

### Python Client
```python
import requests

class TradingBotRAGClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.headers = {"X-API-Key": api_key}
    
    def get_portfolio(self, user_id):
        r = requests.get(
            f"{self.base_url}/rag/portfolio/{user_id}",
            headers=self.headers
        )
        return r.json()
    
    def get_trade_rationale(self, symbol, days=7):
        r = requests.get(
            f"{self.base_url}/rag/trade-decisions/{symbol}",
            params={"days": days},
            headers=self.headers
        )
        return r.json()

# Usage
client = TradingBotRAGClient("http://localhost:8000", "your-api-key")
portfolio = client.get_portfolio("user123")
```

### LangChain Tool
```python
from langchain.tools import Tool

def get_portfolio_tool(user_id):
    client = TradingBotRAGClient(...)
    data = client.get_portfolio(user_id)
    return f"Portfolio: {data['total_value']} USD, P&L: {data['total_unrealized_pl']}"

portfolio_tool = Tool(
    name="get_portfolio",
    func=lambda uid: get_portfolio_tool(uid),
    description="Get user's stock portfolio and P&L"
)
```

## Testing

```bash
# Test all RAG endpoints
curl -H "X-API-Key: your-key" http://localhost:8000/rag/strategies

curl -H "X-API-Key: your-key" http://localhost:8000/rag/ohlcv/AAPL?days=7

curl -H "X-API-Key: your-key" http://localhost:8000/rag/positions

curl -H "X-API-Key: your-key" http://localhost:8000/rag/fundamentals/AAPL
```

## Next Steps
1. Deploy Trading Bot with RAG endpoints
2. Create `rag_reader` database user
3. Integrate RAG service using HTTP API or direct DB
4. Test with sample queries
