from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(
    '/opt/rednun/google_credentials.json',
    scopes=['https://www.googleapis.com/auth/drive'],
    redirect_uri='urn:ietf:wg:oauth:2.0:oob'
)
auth_url, _ = flow.authorization_url(prompt='consent')
print(f"\nOpen this URL in your browser:\n\n{auth_url}\n")
code = input("Paste the authorization code here: ")
flow.fetch_token(code=code)
import pickle
with open('/opt/rednun/google_token.pickle', 'wb') as f:
    pickle.dump(flow.credentials, f)
print("Token saved!")
