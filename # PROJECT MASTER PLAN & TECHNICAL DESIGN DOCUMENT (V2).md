\# PROJECT MASTER PLAN & TECHNICAL DESIGN DOCUMENT (V2)

\#\# 1\. EXECUTIVE SUMMARY  
We are building a \*\*B2B2C Embedded Crypto-as-a-Service MVP\*\*.  
This system allows bank customers to buy/sell crypto within their banking app.  
We will simulate the ecosystem using a Monorepo with 3 services:  
1\. \*\*Mock Exchange:\*\* Simulates Binance.  
2\. \*\*Mock Bank:\*\* Simulates the Client.  
3\. \*\*Core Engine:\*\* The Product (Ledger, Order Matching, Wallet Management).

\---

\#\# 2\. PROJECT STRUCTURE (Monorepo)  
The AI must strictly follow this folder structure to ensure separation of concerns.

\`\`\`text  
/root  
  /apps  
    /mock-exchange  (NestJS \- Port 3001\)  
    /mock-bank      (NestJS \- Port 3002\)  
    /core-engine    (NestJS \- Port 3000\)  
  /libs  
    /shared-types   (Shared DTOs and Interfaces)  
  /prisma           (Database Schema)  
  package.json  
  docker-compose.yml

---

## **3\. API CONTRACTS & PAYLOADS (Strict Rules)**

*All services must strictly adhere to these JSON structures. Do not improvise.*

### **A. Mock Exchange (Provider)**

**Endpoint:** `GET /ticker?symbol=AZNUSDT`

**Response:**  
JSON  
{  
  "symbol": "AZNUSDT",  
  "price": "1.7050",  
  "timestamp": 1715000000  
}

* 

**Endpoint:** `POST /order`

**Request Body:**  
JSON  
{  
  "symbol": "AZNUSDT",  
  "side": "BUY",  
  "quantity": "50.00",  
  "type": "MARKET"  
}

* 

**Response:**  
JSON  
{  
  "orderId": "ex\_12345",  
  "status": "FILLED",  
  "executedPrice": "1.7050",  
  "executedQty": "50.00"  
}

* 

### **B. Core Engine (The Product)**

**Endpoint:** `POST /api/v1/trade/buy`

* **Headers:**  
  * `x-api-key`: (Bank's API Key)  
  * `x-idempotency-key`: (Unique UUID to prevent double charging)

**Request Body:**  
JSON  
{  
  "bankUserId": "user\_123",  
  "quoteId": "optional\_quote\_id",  
  "amount": "100.00",  
  "currency": "AZN"   
}

* 

**Success Response (201 Created):**  
JSON  
{  
  "success": true,  
  "data": {  
    "transactionId": "tx\_999",  
    "status": "COMPLETED",  
    "cryptoAmount": "58.47",  
    "cryptoCurrency": "USDT",  
    "exchangeRate": "1.7100"  
  }  
}

* 

**Error Response (4xx/5xx):**  
JSON  
{  
  "success": false,  
  "errorCode": "INSUFFICIENT\_FUNDS",  
  "message": "User AZN balance is too low."  
}

* 

---

## **4\. DATABASE SCHEMA (PostgreSQL)**

*Use `Decimal` type for all money fields. Precision: 20, Scale: 8\.*

1. **User**  
   * `id` (UUID, PK)  
   * `bankCustomerId` (String, Unique)  
   * `kycLevel` (Int, Default: 1\)  
2. **Account**  
   * `id` (UUID, PK)  
   * `userId` (FK)  
   * `currency` (Enum: AZN, USDT)  
   * `balance` (Decimal)  
   * `lockedBalance` (Decimal) — *For handling pending trades*  
   * `version` (Int) — *For Optimistic Locking*  
3. **Transaction**  
   * `id` (UUID, PK)  
   * `idempotencyKey` (String, Unique) — *Critical for safety*  
   * `type` (Enum: BUY, SELL)  
   * `status` (Enum: PENDING, COMPLETED, FAILED)  
   * `amountIn` (Decimal)  
   * `amountOut` (Decimal)  
   * `feeAmount` (Decimal)  
4. **Ledger** (Immutable)  
   * `id` (PK)  
   * `transactionId` (FK)  
   * `accountId` (FK)  
   * `amount` (Decimal) — *Positive for Credit, Negative for Debit*  
   * `balanceAfter` (Decimal)

---

## **5\. BUSINESS LOGIC & RULES**

### **5.1. The "Spread" Logic**

* Core Engine must fetch the price from Mock Exchange (e.g., 1.70).  
* Apply a dynamic spread (e.g., \+1.5%).  
* Execution Price \= `Market Price * 1.015`.  
* **Note:** The spread is our profit. It must be logged.

### **5.2. Idempotency (Safety)**

* If Core Engine receives a request with an existing `x-idempotency-key`:  
  * It MUST NOT execute the trade again.  
  * It MUST return the result of the original transaction immediately.

### **5.3. Double-Entry Integrity**

* Every trade involves 4 Ledger entries:  
  1. User AZN Account \-\> DEBIT (-)  
  2. System AZN Omnibus \-\> CREDIT (+)  
  3. System USDT Omnibus \-\> DEBIT (-)  
  4. User USDT Account \-\> CREDIT (+)  
* These must happen in a single Database Transaction (ACID).

---

## **6\. ERROR HANDLING STRATEGY**

The AI must implement specific error classes:

* `InsufficientLiquidityError`: When Mock Exchange rejects order or Mock Exchange is down.  
* `SlippageError`: If price moves too much between Quote and Execute.  
* `BankConnectionError`: If Mock Bank doesn't acknowledge.

---

## **7\. EXECUTION PHASES (For the AI)**

1. **Scaffold:** Create the NestJS Monorepo structure.  
2. **Database:** Generate Prisma Client & Migrations.  
3. **Mocking:** Build the Mock Exchange API.  
4. **Core Domain:** Implement `WalletService` and `LedgerService`.  
5. **Trading Flow:** Implement the `BuyUseCase` with the API contracts above.  
6. **E2E Test:** Write a script in Mock Bank to trigger the flow.

