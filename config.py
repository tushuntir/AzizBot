import os

SOCKS5_LIST_URL = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"
HTTP_LIST_URL = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"

PROXY_LIMIT_PER_TYPE = int(os.getenv("PROXY_LIMIT_PER_TYPE", "500"))
TEST_URL = os.getenv("PROXY_TEST_URL", "https://www.youtube.com")
TEST_TIMEOUT = int(os.getenv("PROXY_TEST_TIMEOUT", "10"))
TEST_BATCH_SIZE = int(os.getenv("PROXY_TEST_BATCH_SIZE", "100"))
FETCH_RETRIES = int(os.getenv("PROXY_FETCH_RETRIES", "3"))
REFRESH_INTERVAL = int(os.getenv("PROXY_REFRESH_INTERVAL", "3600"))
RESORT_INTERVAL = int(os.getenv("PROXY_RESORT_INTERVAL", "1800"))
RECENT_USE_WINDOW = int(os.getenv("PROXY_RECENT_USE_WINDOW", "300"))
MAX_SUCCESSES_PER_PROXY = int(os.getenv("PROXY_MAX_SUCCESSES", "10"))
MAX_DOWNLOAD_RETRIES = int(os.getenv("PROXY_MAX_DOWNLOAD_RETRIES", "5"))
WORKING_PROXIES_FILE = os.getenv("WORKING_PROXIES_FILE", "working_proxies.txt")
