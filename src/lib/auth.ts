import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || "https://ldeofktbvacgjsfdexgg.supabase.co";
const supabaseKey = import.meta.env.VITE_SUPABASE_ANON_KEY || "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxkZW9ma3RidmFjZ2pzZmRleGdnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODg2MDgzMzQsImV4cCI6MjEwNDE4NDMzNH0.41cwLg39N9nfpw4UIjwumRRvUAFuQV_ykoqLLZu6JRE";

export const supabase = createClient(supabaseUrl, supabaseKey);
