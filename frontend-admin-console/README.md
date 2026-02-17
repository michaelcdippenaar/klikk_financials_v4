# Klikk Admin Console

Vue.js + Quasar admin console for managing Xero data processes, viewing results, and comparing reports.

## Features

- **Process Management**: Run Xero data sync, processing, and reconciliation operations
- **Data Viewing**: View trail balance, line items, summaries, and reconciliation results
- **Comparison Tools**: Compare Xero reports (P&L, Balance Sheet) to constructed trail balance
- **JWT Authentication**: Secure access with JWT tokens

## Tech Stack

- Vue 3 (Composition API)
- Quasar Framework v2
- Pinia (state management)
- Axios (HTTP client)
- Vue Router

## Setup

```bash
# Install dependencies
npm install

# Start development server
npm run dev

# Build for production
npm run build
```

## Configuration

The API base URL is configured in `src/utils/constants.js`. Default is `http://127.0.0.1:3000` for development.

## Project Structure

```
src/
├── api/              # API client layer
├── components/       # Reusable components
├── layouts/          # Quasar layouts
├── pages/            # Main pages
├── router/           # Vue Router config
├── stores/           # Pinia stores
└── utils/            # Helpers and constants
```
