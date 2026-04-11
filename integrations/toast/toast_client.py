"""
Toast API Client
Handles authentication and all API interactions with Toast POS.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


class ToastAPIClient:
    """Client for interacting with the Toast POS REST API."""

    def __init__(self):
        self.base_url = os.getenv("TOAST_API_BASE_URL", "https://ws-api.toasttab.com")
        self.client_id = os.getenv("TOAST_CLIENT_ID")
        self.client_secret = os.getenv("TOAST_CLIENT_SECRET")
        self.restaurants = {
            "dennis": os.getenv("TOAST_RESTAURANT_GUID_DENNIS"),
            "chatham": os.getenv("TOAST_RESTAURANT_GUID_CHATHAM"),
        }
        self._token = None
        self._token_expiry = 0

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "TOAST_CLIENT_ID and TOAST_CLIENT_SECRET must be set in .env"
            )

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_token(self):
        """Authenticate and retrieve an access token."""
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        url = f"{self.base_url}/authentication/v1/authentication/login"
        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        headers = {"Content-Type": "application/json"}

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        self._token = data["token"]["accessToken"]
        self._token_expiry = time.time() + 23 * 3600
        logger.info("Toast API token refreshed successfully")
        return self._token

    def _headers(self, restaurant_guid):
        """Build request headers with auth token and restaurant context."""
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Toast-Restaurant-External-ID": restaurant_guid,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Generic request with retry and rate-limit handling
    # ------------------------------------------------------------------

    def _get(self, path, restaurant_guid, params=None, max_retries=3):
        """Make a GET request with automatic retry on rate limits."""
        url = f"{self.base_url}{path}"
        headers = self._headers(restaurant_guid)

        for attempt in range(max_retries):
            try:
                resp = requests.get(
                    url, headers=headers, params=params, timeout=60
                )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(
                        f"Rate limited. Retrying in {retry_after}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(retry_after)
                    continue

                if resp.status_code == 400:
                    logger.error(f"400 Bad Request for {url} params={params}")
                    logger.error(f"Response body: {resp.text[:500]}")
                    resp.raise_for_status()

                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.RequestException as e:
                if attempt == max_retries - 1:
                    logger.error(f"API request failed after {max_retries} attempts: {e}")
                    raise
                time.sleep(2 ** attempt)

    # ------------------------------------------------------------------
    # Restaurant Info
    # ------------------------------------------------------------------

    def get_restaurant_info(self, location="dennis"):
        """Get general restaurant configuration."""
        guid = self.restaurants[location]
        return self._get("/restaurants/v1/restaurants", guid)

    # ------------------------------------------------------------------
    # Orders API
    # ------------------------------------------------------------------

    def get_orders_bulk(self, location, start_date=None, end_date=None,
                        page_size=100, page=1):
        """
        Retrieve orders in bulk using startDate/endDate (ISO format).
        Toast standard API requires startDate/endDate with ISO timestamps.
        Max time window per request is variable; we use 1 hour chunks.
        
        Args:
            location: 'dennis' or 'chatham'
            start_date: ISO datetime string (e.g., '2026-01-14T00:00:00.000+0000')
            end_date: ISO datetime string
            page_size: Number of orders per page (max 100)
            page: Page number (1-indexed)
        """
        guid = self.restaurants[location]
        params = {
            "pageSize": page_size,
            "page": page,
            "startDate": start_date,
            "endDate": end_date,
        }

        return self._get("/orders/v2/ordersBulk", guid, params=params)

    def get_all_orders_for_date(self, location, date_obj):
        """
        Fetch ALL orders for a business date by querying in time chunks.
        Uses startDate/endDate with ISO timestamps.
        Queries in 6-hour chunks to stay within Toast's limits.
        
        Args:
            date_obj: a datetime.date object
        """
        from zoneinfo import ZoneInfo
        all_orders = []
        
        # Query in 6-hour chunks across the day (4 AM ET to 4 AM ET next day)
        eastern = ZoneInfo("America/New_York")
        utc = ZoneInfo("UTC")
        local_start = datetime(date_obj.year, date_obj.month, date_obj.day, 4, 0, 0, tzinfo=eastern)
        base_start = local_start.astimezone(utc).replace(tzinfo=None)
        base_end = base_start + timedelta(hours=24)
        
        chunk_hours = 6
        current = base_start
        
        while current < base_end:
            chunk_end = min(current + timedelta(hours=chunk_hours), base_end)
            
            start_iso = current.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            end_iso = chunk_end.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
            
            page = 1
            while True:
                try:
                    orders = self.get_orders_bulk(
                        location, start_date=start_iso, end_date=end_iso,
                        page_size=100, page=page
                    )
                    if not orders:
                        break
                    all_orders.extend(orders)
                    if len(orders) < 100:
                        break
                    page += 1
                    time.sleep(0.3)
                except Exception as e:
                    logger.error(f"Error fetching orders chunk {start_iso}-{end_iso} page {page}: {e}")
                    break
            
            current = chunk_end
            time.sleep(0.3)

        logger.info(
            f"Fetched {len(all_orders)} orders for {location} on {date_obj.strftime('%Y-%m-%d')}"
        )
        return all_orders

    # ------------------------------------------------------------------
    # Labor API
    # ------------------------------------------------------------------

    def get_employees(self, location="dennis"):
        """Get all employees for a location."""
        guid = self.restaurants[location]
        return self._get("/labor/v1/employees", guid)

    def get_time_entries(self, location, start_date, end_date):
        """
        Get time entries (clock in/out) for a date range.
        
        Args:
            start_date: ISO datetime string
            end_date: ISO datetime string
        """
        guid = self.restaurants[location]
        params = {"startDate": start_date, "endDate": end_date}
        return self._get("/labor/v1/timeEntries", guid, params=params)

    def get_jobs(self, location="dennis"):
        """Get all job definitions for a location."""
        guid = self.restaurants[location]
        return self._get("/labor/v1/jobs", guid)

    # ------------------------------------------------------------------
    # Menus API (V2)
    # ------------------------------------------------------------------

    def get_menus(self, location="dennis"):
        """Get all menus and menu items."""
        guid = self.restaurants[location]
        return self._get("/menus/v2/menus", guid)

    def get_menu_item(self, location, item_guid):
        """Get a specific menu item by GUID."""
        guid = self.restaurants[location]
        return self._get(f"/menus/v2/menuItems/{item_guid}", guid)

    # ------------------------------------------------------------------
    # Configuration API
    # ------------------------------------------------------------------

    def get_dining_options(self, location="dennis"):
        """Get dining options (dine-in, takeout, delivery, etc.)."""
        guid = self.restaurants[location]
        return self._get("/config/v2/diningOptions", guid)

    def get_revenue_centers(self, location="dennis"):
        """Get revenue centers."""
        guid = self.restaurants[location]
        return self._get("/config/v2/revenueCenters", guid)

    def get_service_areas(self, location="dennis"):
        """Get service areas (bar, patio, dining room, etc.)."""
        guid = self.restaurants[location]
        return self._get("/config/v2/serviceAreas", guid)

    def get_tax_rates(self, location="dennis"):
        """Get configured tax rates."""
        guid = self.restaurants[location]
        return self._get("/config/v2/taxRates", guid)

    # ------------------------------------------------------------------
    # Cash Management API
    # ------------------------------------------------------------------

    def get_cash_entries(self, location, business_date):
        """Get cash entries for a business date (YYYYMMDD)."""
        guid = self.restaurants[location]
        params = {"businessDate": business_date}
        return self._get("/cashmgmt/v1/entries", guid, params=params)

    def get_cash_deposits(self, location, business_date):
        """Get cash deposits for a business date (YYYYMMDD)."""
        guid = self.restaurants[location]
        params = {"businessDate": business_date}
        return self._get("/cashmgmt/v1/deposits", guid, params=params)
