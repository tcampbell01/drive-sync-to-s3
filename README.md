# drive-sync-to-s3

# 1. Clean up
rm -rf lambda_build
rm -f lambda_package.zip

# 2. Create build directory
mkdir -p lambda_build

# 3. Copy your app.py
cp lambda/app.py lambda_build/app.py

# 4. Install dependencies directly into lambda_build (NOT lambda_build/app)
python3.11 -m pip install \
  -t lambda_build \
  google-api-python-client \
  google-auth \
  google-auth-oauthlib \
  google-auth-httplib2

# 5. Create zip
cd lambda_build
zip -r ../lambda_package.zip .
cd ..


Copy
The structure should be:

lambda_build/
├── app.py
├── google/
├── googleapiclient/
└── other dependencies...

Copy
Not:

lambda_build/
├── app.py
└── app/
    ├── google/
    └── googleapiclient/


Google Drive → AWS S3 Sync

Deployment Guide for a New Organization (Google + AWS)

This guide explains how to deploy the Drive-to-S3 sync Lambda using:

a different Google Workspace / Drive

a different AWS account

no shared credentials with your personal setup

Architecture Overview (what this deploys)

AWS Lambda
Runs a Python function that reads Google Drive changes and uploads files to S3.

Google Drive API (OAuth)
Provides read-only access to Drive contents.

AWS S3
Stores a mirrored copy of Drive files (one object per Drive file, overwritten on update).

AWS Secrets Manager
Stores Google OAuth credentials securely.

AWS Systems Manager (SSM) Parameter Store
Stores the Drive startPageToken for incremental sync.

Amazon EventBridge Scheduler
Runs the Lambda automatically (daily / weekly).

Part 1 — Google Cloud setup (Synagogue Google account)

These steps must be done using the synagogue’s Google account (or an admin-approved account).

1. Create a Google Cloud project

Go to: https://console.cloud.google.com/

Click Select project → New Project

Name it something like:

synagogue-drive-backup


Create the project and select it.

2. Enable Google Drive API

Go to APIs & Services → Library

Search for Google Drive API

Click Enable

3. Create OAuth credentials

Go to APIs & Services → Credentials

Click Create Credentials → OAuth client ID

If prompted, configure the OAuth consent screen:

User type: Internal (preferred) or External

App name: Drive Backup

Scopes: none yet

Save

Create OAuth Client ID:

Application type: Desktop App

Name: drive-backup-client

Download the client JSON file
(This file contains client_id and client_secret)

4. Generate a refresh token (one-time)

On a local machine (not Lambda):

Place the downloaded OAuth client JSON somewhere safe.

Run the provided local auth script (or equivalent):

Authorize with the synagogue Google account

Approve Drive read-only access

Capture:

refresh_token

client_id

client_secret

⚠️ This authorization step only happens once.

Part 2 — AWS setup (Synagogue AWS account)

These steps must be done inside the synagogue’s AWS account.

5. Create S3 bucket

Go to S3 → Create bucket

Bucket name (example):

synagogue-google-drive-backup


Region: us-east-1

Block public access: ON

(Optional) Enable Versioning if you want file history.

6. Create Secrets Manager secret (Google OAuth)

Go to Secrets Manager → Store a new secret

Secret type: Other type of secret

Secret value (JSON):

{
  "token": {
    "client_id": "GOOGLE_CLIENT_ID",
    "client_secret": "GOOGLE_CLIENT_SECRET",
    "refresh_token": "GOOGLE_REFRESH_TOKEN",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/drive.readonly"]
  }
}


Secret name:

drivesync/google-oauth


Save.

7. Create SSM Parameter

Go to Systems Manager → Parameter Store

Create parameter:

Name:

/drivesync/startPageToken


Type: String

Value:

INIT

8. Create IAM role for Lambda

Go to IAM → Roles → Create role

Trusted entity: AWS service

Service: Lambda

Attach policies:

AWSLambdaBasicExecutionRole

Custom inline policy (below)

Inline policy (minimum required)
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:drivesync/google-oauth*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:PutParameter"
      ],
      "Resource": "arn:aws:ssm:*:*:parameter/drivesync/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::synagogue-google-drive-backup/*"
    }
  ]
}


Role name:

drivesync-lambda-role

Part 3 — Lambda deployment
9. Package the Lambda code

From the repo root:

rm -rf lambda_build lambda_package.zip
mkdir lambda_build

cp lambda/app.py lambda_build/app.py

python3.11 -m pip install -t lambda_build \
  google-api-python-client \
  google-auth \
  google-auth-oauthlib \
  google-auth-httplib2

cd lambda_build
zip -r ../lambda_package.zip .
cd ..

10. Create the Lambda function

Go to Lambda → Create function

Author from scratch:

Name: drive-sync-to-s3

Runtime: Python 3.11

Architecture: x86_64

Execution role: Use existing role

Role: drivesync-lambda-role

Upload code:

Upload lambda_package.zip

Runtime settings:

Handler:

app.handler


Timeout:

2–5 minutes

Memory:

512–1024 MB

11. First run (initialization)

Click Test

Run with empty event {}

Expected output:

{
  "status": "initialized"
}


This saves the Drive startPageToken.

Part 4 — Scheduling
12. Create EventBridge schedule

Go to EventBridge → Scheduler

Create schedule:

Schedule type: Recurring

Cron example (daily 9:30am Eastern):

cron(30 13 * * ? *)


Flexible window: Off

Target:

Lambda function: drive-sync-to-s3

Payload:

{}


Execution role:

Create or select a role that allows:

lambda:InvokeFunction

Part 5 — Operational behavior (important)
What the sync does

Uploads one S3 object per Drive file

Overwrites on edit (no duplicates)

Skips non-exportable Google types (Forms, Sites, etc.)

Exports Docs/Sheets/Slides to Office formats

Preserves folder structure as:

drivesync/My Drive_<folder>/<file>__<fileId>

What it does NOT do

It does not delete files from S3 if removed from Drive

It does not backfill old files automatically

Only changes after initialization

(Backfill can be added later if needed.)

Part 6 — Cost considerations (synagogue)

Typical monthly cost (small org):

Lambda: ~$0 (within free tier)

S3: storage only (a few dollars/month)

Secrets Manager: ~$0.40/month

EventBridge: ~$0

This will not meaningfully increase AWS costs.

Final notes (important for handoff)

Google OAuth refresh token never expires unless revoked

AWS Secrets Manager keeps credentials secure

Lambda runs without human interaction

This setup is safe to run daily or weekly

