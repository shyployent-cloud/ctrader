"""
OAuth Callback App
==================
A tiny Flask web server deployed on Railway whose sole purpose is to
catch the cTrader OAuth redirect and display the authorization code
clearly on screen so you can copy it.

This runs ONCE during initial setup only. After you have your access
token and refresh token, this app stays running on Railway but is
never used again - it just sits idle at zero cost.

Usage:
  1. Deploy this to Railway (see setup guide)
  2. Set your cTrader redirect URL to:
     https://your-app-name.up.railway.app/callback
  3. Complete the OAuth flow in your browser
  4. This page displays your authorization code clearly
  5. Copy the code and run the token exchange in Colab
"""

from flask import Flask, request
import os

app = Flask(__name__)

@app.route("/")
def home():
    return """
    <html>
    <body style="font-family: Arial; padding: 40px; background: #1a1a2e; color: white;">
        <h2>Wave Trader - OAuth Callback Server</h2>
        <p>Server is running. Complete the cTrader authorization flow to get your code.</p>
        <p>Your redirect URL for cTrader is:</p>
        <code style="background: #16213e; padding: 10px; display: block; margin: 10px 0;">
            https://https://YOUR-APP-NAME.up.railway.app/callback
        </code>
    </body>
    </html>
    """

@app.route("/callback")
def callback():
    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        return f"""
        <html>
        <body style="font-family: Arial; padding: 40px; background: #1a1a2e; color: white;">
            <h2 style="color: #ff4444;">Authorization Failed</h2>
            <p>Error: {error}</p>
            <p>Go back and try the authorization URL again.</p>
        </body>
        </html>
        """

    if not code:
        return """
        <html>
        <body style="font-family: Arial; padding: 40px; background: #1a1a2e; color: white;">
            <h2 style="color: #ff4444;">No Code Received</h2>
            <p>The authorization code was not found in the redirect.</p>
            <p>Make sure your redirect URL in cTrader matches this app's /callback endpoint exactly.</p>
        </body>
        </html>
        """

    return f"""
    <html>
    <body style="font-family: Arial; padding: 40px; background: #1a1a2e; color: white;">
        <h2 style="color: #00ff88;">Authorization Code Received!</h2>
        <p>Copy the code below and paste it into your Colab token exchange cell:</p>
        <div style="background: #16213e; border: 2px solid #00ff88; padding: 20px;
                    border-radius: 8px; margin: 20px 0; word-break: break-all;">
            <strong style="font-size: 18px;">{code}</strong>
        </div>
        <p style="color: #ffaa00;">⚠️ This code expires in a few minutes — copy it now and run the Colab cell immediately.</p>
        <p style="color: #aaaaaa; font-size: 14px;">Once you have exchanged this code for tokens in Colab,
        you will not need to repeat this process.</p>
    </body>
    </html>
    """

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

