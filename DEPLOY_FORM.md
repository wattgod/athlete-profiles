# How to Deploy the Athlete Questionnaire Form

## Option 1: GitHub Pages (Recommended)

### Step 1: Push the form to your repository

```bash
cd ~/athlete-profiles

# Add and commit the form
git add docs/athlete-questionnaire.html
git commit -m "Add athlete questionnaire form"
git push
```

### Step 2: Enable GitHub Pages

1. Go to your repository on GitHub: `https://github.com/[your-username]/athlete-profiles`
2. Click **Settings** → **Pages** (left sidebar)
3. Under **Source**, select:
   - **Branch**: `main` (or `docs` if you have a docs branch)
   - **Folder**: `/docs`
4. Click **Save**

### Step 3: Access your form

After a few minutes, your form will be available at:
```
https://[your-username].github.io/athlete-profiles/athlete-questionnaire.html
```

**Note**: It may take 1-2 minutes for GitHub Pages to build and deploy.

---

## Option 2: Deploy to Existing Landing Page Project

If you want the form on the same domain as `survey.html`:

### Step 1: Copy the form

```bash
cp ~/athlete-profiles/docs/athlete-questionnaire.html \
   ~/gravel-landing-page-project/docs/
```

### Step 2: Commit and push

```bash
cd ~/gravel-landing-page-project

git add docs/athlete-questionnaire.html
git commit -m "Add athlete questionnaire form"
git push
```

### Step 3: Access your form

Your form will be available at:
```
https://wattgod.github.io/gravel-landing-page-project/athlete-questionnaire.html
```

---

## Option 3: Custom Domain / Web Server

If you have your own web server or custom domain:

### Step 1: Upload the file

```bash
# Using SCP
scp docs/athlete-questionnaire.html user@your-server.com:/var/www/html/

# Or using FTP/SFTP client
# Upload docs/athlete-questionnaire.html to your web root
```

### Step 2: Update webhook URL

Edit the form and update the webhook URL:

```javascript
// In athlete-questionnaire.html, find this line:
const webhookUrl = 'https://your-webhook-endpoint.com/athlete-intake';

// Replace with your actual webhook URL
```

---

## Quick Test

After deploying, test the form:

1. Open the form URL in your browser
2. Fill out a test submission
3. Check browser console (F12) for any errors
4. Verify form submits correctly

---

## Troubleshooting

### Form doesn't load
- Check file path is correct
- Verify GitHub Pages is enabled
- Wait 1-2 minutes for GitHub Pages to build

### Form loads but styling is broken
- Check browser console for CSS errors
- Verify all CSS is inline (it should be - no external files)

### Form submits but nothing happens
- Check webhook URL is set correctly
- Verify webhook service is active
- Check browser console for JavaScript errors

---

## Next Steps

After deploying the form:

1. ✅ Form is accessible at public URL
2. ⏭️ Set up webhook service (Zapier/Make.com)
3. ⏭️ Configure GitHub secrets
4. ⏭️ Test end-to-end workflow

See `ATHLETE_OS_PUBLIC_INTAKE.md` for complete setup instructions.

