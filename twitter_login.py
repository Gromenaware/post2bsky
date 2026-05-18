import argparse
from playwright.sync_api import sync_playwright
from tweety import Twitter
import time

def get_twitter_auth_token(username, password):
    with sync_playwright() as p:
        print("🚀 Launching headless browser...")
        # Add arguments to bypass basic bot detection
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        try:
            print("🌐 Navigating to X login...")
            page.goto("https://x.com/i/flow/login")
            
            # 1. Fill with username
            print(f"👤 Entering username: {username}...")
            # Using the exact autocomplete attribute from your HTML
            page.wait_for_selector('input[autocomplete="username"]', timeout=25000)
            time.sleep(10)  # Wait a bit for any potential animations or dynamic content to load
            # CLICK ON THE INPUT BEFORE POPULATING THE USERNAME
            page.click('input[autocomplete="username"]')
            time.sleep(10)
            page.fill('input[autocomplete="username"]', username)
            
            # Enter and wait for the next screen
            print("➡️ Pressing Enter to proceed...")
            page.keyboard.press("Enter")
            
            # Wait a moment for the screen transition animation
            time.sleep(10)
            
            # 2. Fill with the password
            print("🔑 Entering password...")
            # Using the exact name attribute from your HTML
            page.wait_for_selector('input[name="password"]', timeout=25000)
            time.sleep(10)  # Wait a bit for any potential animations or dynamic content to load
            # CLICK ON THE INPUT BEFORE POPULATING THE PASSWORD
            page.click('input[name="password"]')
            page.fill('input[name="password"]', password)
            
            # 3. Click on Login
            print("🖱️ Clicking 'Log in'...")
            # Targeting the exact span text you provided
            page.locator('span:has-text("Log in")').first.click()
            
            print("⏳ Waiting for login to complete...")
            # Wait for the redirect to the home page
            page.wait_for_url("**/home", timeout=25000)
            
            # 4. Extract Cookies
            cookies = context.cookies()
            auth_token = None
            for cookie in cookies:
                if cookie['name'] == 'auth_token':
                    auth_token = cookie['value']
                    break
                    
            browser.close()
            return auth_token
            
        except Exception as e:
            # IF IT FAILS, TAKE A SCREENSHOT!
            print(f"❌ Error encountered. Taking a screenshot...")
            page.screenshot(path="error_screenshot.png")
            browser.close()
            raise e

def main():
    # Set up argument parsing
    parser = argparse.ArgumentParser(description="Automate Twitter login and save session token.")
    parser.add_argument("username", help="Your Twitter username")
    parser.add_argument("password", help="Your Twitter password")
    
    args = parser.parse_args()

    try:
        # Pass the arguments from the terminal to the function
        token = get_twitter_auth_token(args.username, args.password)
        
        if token:
            print("🔑 Successfully extracted auth_token!")
            
            # Initialize the tweety session file
            app = Twitter("my_account_session")
            
            print("💾 Saving session to tweety-ns...")
            app.load_auth_token(token)
            
            print("✅ Success! Your session has been saved.")
        else:
            print("❌ Failed to find auth_token cookie.")
            
    except Exception as e:
        print(f"❌ An error occurred: {e}")
        print("📸 Check the 'error_screenshot.png' file in your folder to see what X showed the bot!")

if __name__ == "__main__":
    main()