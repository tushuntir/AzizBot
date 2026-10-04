import asyncio
import aiohttp
import time
from typing import List, Tuple

async def test_proxy(proxy: str, timeout: int = 10) -> Tuple[str, bool, float]:
    """Test a single proxy, return (proxy, is_working, response_time)"""
    start = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'http://www.google.com',
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                elapsed = time.time() - start
                return (proxy, response.status == 200, elapsed)
    except Exception as e:
        return (proxy, False, time.time() - start)

async def test_all_proxies(proxies: List[str], batch_size: int = 100) -> List[Tuple[str, bool, float]]:
    """Test all proxies in batches"""
    results = []
    
    # Process in batches to avoid overwhelming
    for i in range(0, len(proxies), batch_size):
        batch = proxies[i:i + batch_size]
        tasks = [test_proxy(proxy) for proxy in batch]
        batch_results = await asyncio.gather(*tasks)
        results.extend(batch_results)
        
        # Small delay between batches
        if i + batch_size < len(proxies):
            await asyncio.sleep(0.5)
    
    return results

# Usage
async def main():
    # Get proxies
    import requests
    url = "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"
    proxies = requests.get(url, timeout=10).text.strip().split('\n')[:500]
    proxies = [f"socks5://{p}" for p in proxies if p]
    
    print(f"Testing {len(proxies)} proxies...")
    start = time.time()
    
    # Test all
    results = await test_all_proxies(proxies, batch_size=100)
    
    # Filter working ones
    working = [(p, t) for p, ok, t in results if ok]
    working.sort(key=lambda x: x[1])  # Sort by speed
    
    elapsed = time.time() - start
    print(f"\nTested {len(proxies)} proxies in {elapsed:.2f}s")
    print(f"Working: {len(working)} ({len(working)/len(proxies)*100:.1f}%)")
    
    # Show top 10 fastest
    print("\nTop 10 fastest proxies:")
    for proxy, speed in working[:10]:
        print(f"  {proxy} — {speed:.2f}s")
    
    # Save to file
    with open('working_proxies.txt', 'w') as f:
        for proxy, speed in working:
            f.write(f"{proxy}\n")

asyncio.run(main())
