# Setup Instructions

## Prerequisites

- Node.js >= 18.0.0
- npm >= 9.0.0

## Installation

```bash
# Install dependencies
npm install

# Copy environment file
cp .env.example .env

# Edit .env and set your API base URL if different from default
# VITE_API_BASE_URL=http://127.0.0.1:3000
```

## Development

```bash
# Start development server (runs on http://localhost:9000)
npm run dev
```

## Building for Production

```bash
# Build for production
npm run build

# The built files will be in dist/ folder
```

## Project Structure

```
frontend-admin-console/
├── src/
│   ├── api/              # API client and endpoints
│   ├── components/       # Reusable Vue components
│   ├── layouts/          # Quasar layouts
│   ├── pages/            # Page components
│   ├── router/           # Vue Router configuration
│   ├── stores/           # Pinia stores (state management)
│   └── utils/            # Utility functions and constants
├── quasar.conf.js        # Quasar configuration
└── package.json          # Dependencies and scripts
```

## Features

- **Authentication**: JWT-based login with token refresh
- **Process Management**: Run Xero data sync and processing operations
- **Data Viewing**: View trail balance, line items, and summaries
- **Comparison**: Compare Xero reports to constructed trail balance

## API Integration

The app connects to the Django REST API backend. Make sure the backend is running and accessible at the configured API_BASE_URL.

Default API URL: `http://127.0.0.1:3000`

## Notes

- All API endpoints currently use `AllowAny` permission in the backend, but JWT authentication is implemented in the frontend
- The app requires a tenant to be selected before most operations can be performed
- Process results are displayed in JSON format for debugging
