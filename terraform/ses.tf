# Single-user project — sender and recipient are the same verified address.
# SES starts in sandbox mode (both identities must be verified, 200
# emails/day cap) which is more than enough for one email/week; no need to
# request production access at this scale.
resource "aws_ses_email_identity" "notification" {
  email = var.notification_email
}
