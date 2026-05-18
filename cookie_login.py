from tweety import Twitter

# This creates a local session file so you only log in once
app = Twitter("my_account_session")

# Log in with your credentials
app.sign_in("grijanderm59258", "Bo3usi!!")

# Fetch the tweets
tweets = app.get_tweets("meteocat", pages=1)
for tweet in tweets:
    print(tweet.text)
