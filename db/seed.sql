-- Three TEST designers. Replace telegram_chat_id / calendar_id with real values
-- (each designer must /start the bot once so it can message them privately).
insert into designers (name, telegram_chat_id, calendar_id, areas, handles_commercial, active) values
  ('Test Designer West',  '000000001', 'test-west@group.calendar.google.com',
   array['Kothrud','Baner','Aundh','Wakad','Hinjewadi','Balewadi','Pashan','Bavdhan','Sus','Warje','Shivane','Erandwane','Deccan','Nanded City','Sinhagad Road','Punawale','Tathawade','Ravet'],
   true, true),
  ('Test Designer East',  '000000002', 'test-east@group.calendar.google.com',
   array['Koregaon Park','Kalyani Nagar','Viman Nagar','Kharadi','Hadapsar','Magarpatta','NIBM','Kondhwa','Undri','Wagholi','Yerawada','Camp','Shivajinagar','Katraj','Bibwewadi'],
   false, true),
  ('Test Designer PCMC',  '000000003', 'test-pcmc@group.calendar.google.com',
   array['Pimpri','Chinchwad','Pimple Saudagar','Pimple Nilakh','Pimple Gurav','Akurdi','Nigdi','Moshi','Wakad','Ravet','Punawale','Tathawade'],
   true, true);
