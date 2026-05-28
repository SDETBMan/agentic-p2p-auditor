# Purchase-to-Pay (P2P) API Specification

This document describes the REST API for the Purchase-to-Pay lifecycle: vendor onboarding, purchase orders, goods receipt, invoicing, matching, approval, and vendor exposure reporting.

## Conventions

- **Base path**: All routes are relative to the API host (e.g. `https://api.example.com`).
- **Content type**: `application/json` unless noted.
- **Identifiers**: `{id}` path parameters are opaque resource identifiers (strings).
- **Errors**: Failed requests return a JSON body with `code`, `message`, and optional `details`; HTTP status reflects the failure class (4xx client, 5xx server).

---

## Vendors

### `GET /vendors`

Lists vendors with optional filtering and pagination.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Discover and search vendor master records. |
| **Query params** | Typical: `status`, `search`, `page`, `page_size` (implementation-defined). |
| **Response** | `200 OK` — collection of vendor summaries (e.g. `id`, `name`, `status`, `tax_id`). |

### `POST /vendors`

Creates a new vendor.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Register a vendor for purchasing and payment. |
| **Request body** | Vendor attributes: legal name, identifiers, payment terms, default GL, banking (if applicable), contact, status workflow fields. |
| **Response** | `201 Created` — vendor resource including `id`; `Location` header may point to the new resource. |

### `GET /vendors/{id}/exposure`

Returns aggregated financial exposure for a vendor.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Support credit/risk and treasury views: open PO value, received-not-invoiced, approved-not-paid invoices, etc. |
| **Response** | `200 OK` — structured totals (e.g. `open_po_amount`, `grni_amount`, `approved_payable_amount`, `currency`, `as_of`). |

---

## Purchase orders

### `POST /purchase-orders`

Creates a draft purchase order.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Capture line-level demand against a vendor before commitment. |
| **Request body** | `vendor_id`, header fields (ship-to, bill-to, dates, currency), `lines[]` with item, quantity, unit price, GL/cost object. |
| **Response** | `201 Created` — PO with `id` and status `draft` (or equivalent). |

### `GET /purchase-orders/{id}`

Retrieves a single purchase order.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Read header, lines, statuses, and receipt/match linkage summaries. |
| **Response** | `200 OK` — full PO representation; `404` if not found. |

### `POST /purchase-orders/{id}/submit`

Submits a purchase order for approval or issuance.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Transition from draft to an in-flight state (e.g. pending approval or released to vendor). |
| **Request body** | Optional comment or approval context. |
| **Response** | `200 OK` or `204 No Content` — updated PO status. May return `409 Conflict` if vendor is inactive or business rules fail (see [Financial control rules](#financial-control-rules)). |

### `POST /purchase-orders/{id}/receive`

Records receipt of goods or services against the PO.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Post quantities received (full or partial) to support GRNI and 3-way match. |
| **Request body** | Receipt lines: PO line references, quantities, dates, optional lot/location. |
| **Response** | `200 OK` — receipt document id(s) and updated line receipt status; may set **partial receipt** indicators on lines. |

---

## Invoices

### `POST /invoices`

Creates an invoice (supplier invoice / AP document).

| Aspect | Description |
|--------|-------------|
| **Purpose** | Register an invoice for matching and payment. |
| **Request body** | `vendor_id`, invoice number, dates, currency, amounts, tax, `lines[]` linked to PO lines or accrual categories as required. |
| **Response** | `201 Created` — invoice `id` and status; may reject duplicates (see [Duplicate invoice detection](#6-duplicate-invoice-detection)). |

### `POST /invoices/{id}/match`

Runs or records matching of invoice to PO and receipts.

| Aspect | Description |
|--------|-------------|
| **Purpose** | Enforce **3-way match** (PO, receipt, invoice) within tolerance; produce match status and variances. |
| **Request body** | Optional explicit line mappings or “auto-match” flag. |
| **Response** | `200 OK` — match result: matched/unmatched lines, tolerances, hold reasons; `409` if **3-way match gate** blocks progression. |

### `POST /invoices/{id}/approve`

Approves the invoice for payment (subject to controls).

| Aspect | Description |
|--------|-------------|
| **Purpose** | Final AP approval before payment file / disbursement. |
| **Request body** | Approver identity, optional comment, payment date override if allowed. |
| **Response** | `200 OK` — approved status; `409` if match, vendor, **overpayment**, **GL balance**, or other gates are not satisfied. |

---

## Financial control rules

The API and backing services enforce the following controls. Endpoints above may return `409 Conflict` or structured validation errors when a rule is violated.

### 1. Overpayment protection

Ensures cumulative approved and scheduled payments do not exceed what policy allows for the matched obligation (e.g. invoice matched amount plus approved tolerances). The system blocks approval or payment initiation when payables would exceed matched/authorized amounts, preventing excess disbursement to vendors.

### 2. 3-way match gate

Invoice lines (and header totals where applicable) must align with an issued **purchase order** and confirmed **receipt** within defined tolerances (quantity, price, tax). Until match succeeds, the invoice remains in a pre-approval state; `POST /invoices/{id}/match` is the primary interaction that evaluates this gate.

### 3. Partial receipt flag

When received quantity on a PO line is less than ordered (or receipt is split across documents), lines carry a **partial receipt** indicator. Downstream matching and accrual logic use this flag to prevent treating undelivered quantities as available for full invoice matching and to highlight open receiving liability.

### 4. Inactive vendor gate

Purchase orders must not be submitted (and in many designs, invoices must not be posted) against vendors in **inactive**, **blocked**, or **on-hold** status. `POST /purchase-orders/{id}/submit` and invoice create/approve paths consult vendor master status to enforce this gate.

### 5. GL balance

Before approval or payment, accounting validation ensures debits and credits align with the enterprise chart of accounts and posting rules (e.g. expense/GRNI/AP/tax lines net to a balanced entry in the posting currency). Imbalanced or invalid account combinations are rejected to protect the general ledger.

### 6. Duplicate invoice detection

On `POST /invoices`, the system checks for duplicate supplier invoices using a composite key such as **vendor + invoice number + invoice date** (and optionally amount). Conflicts return a clear error so the same economic event is not paid twice.

---

## Summary of endpoints

| Method | Path |
|--------|------|
| `GET` | `/vendors` |
| `POST` | `/vendors` |
| `GET` | `/vendors/{id}/exposure` |
| `POST` | `/purchase-orders` |
| `GET` | `/purchase-orders/{id}` |
| `POST` | `/purchase-orders/{id}/submit` |
| `POST` | `/purchase-orders/{id}/receive` |
| `POST` | `/invoices` |
| `POST` | `/invoices/{id}/match` |
| `POST` | `/invoices/{id}/approve` |
