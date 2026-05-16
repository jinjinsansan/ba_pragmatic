-- Ensure signup referral code from auth metadata is persisted to profiles.referred_by
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
  meta_referred_by TEXT;
BEGIN
  meta_referred_by := NULLIF(TRIM(COALESCE(NEW.raw_user_meta_data->>'referred_by', '')), '');
  INSERT INTO profiles (id, email, referral_code, referred_by)
  VALUES (
    NEW.id,
    NEW.email,
    'REF-' || UPPER(SUBSTR(MD5(NEW.id::TEXT), 1, 8)),
    meta_referred_by
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
