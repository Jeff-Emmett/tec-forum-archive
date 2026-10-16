u = User.find_by_email("jeff@jeffemmett.com") ||
    User.new(email: "jeff@jeffemmett.com", username: "tec_archivist", name: "Archive admin")
u.password = SecureRandom.base64(48) if u.new_record? # never printed; set a real one with rake admin:create
u.active = true
u.approved = true
u.save!
u.grant_admin!
u.change_trust_level!(TrustLevel[4])
u.activate
u.email_tokens.update_all(confirmed: true)
puts "admin: #{u.username} admin=#{u.admin}"
