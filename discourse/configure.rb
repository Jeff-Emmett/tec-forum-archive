# frozen_string_literal: true
#
# One-time setup after import_tec.rb: site identity, read-only posture, seed cleanup.
#   su discourse -c 'RAILS_ENV=production bundle exec rails runner /shared/import/configure.rb'
# Safe to re-run.

ARCHIVE = "https://tec-archive.mycostack.xyz"
REPO = "https://gitea.jeffemmett.com/jeffemmett/tec-forum-archive"

# Seed content a fresh Discourse ships with that the TEC forum never had.
%w[general site-feedback].each do |slug|
  next unless (c = Category.find_by(slug: slug))
  next if Topic.where(category_id: c.id).where("id >= 19").exists?
  Topic.where(category_id: c.id).each { |t| t.posts.each(&:destroy!); t.destroy! }
  SiteSetting.general_category_id = -1 if slug == "general" && SiteSetting.respond_to?(:general_category_id)
  c.destroy!
end
Topic.where(id: 5).where("title LIKE 'Welcome to Discourse%'").each { |t| t.posts.each(&:destroy!); t.destroy! }

settings = {
  title: "TEC Forum Archive",
  site_description: "Read-only archive of forum.tecommons.org, the Token Engineering Commons forum, rebuilt from public web-archive captures.",
  short_site_description: "Token Engineering Commons forum, archived",
  contact_email: "jeff@jeffemmett.com",
  allow_new_registrations: false,
  disable_emails: "yes",
  download_remote_images_to_local: false,
  max_oneboxes_per_post: 0,
  wizard_enabled: false,
  bootstrap_mode_enabled: false,
  login_required: false,
  max_category_nesting: 3,
  global_notice: "Read-only archive of <b>forum.tecommons.org</b>, which went offline with the TEC DAO&#39;s shutdown. " \
                 "Rebuilt from Wayback Machine and Common Crawl captures; some posts were never captured. " \
                 "Sources, gaps and data: <a href=\"#{ARCHIVE}\">evidence view</a> · <a href=\"#{REPO}\">source</a>.",
}
settings.each do |k, v|
  next unless SiteSetting.respond_to?(k)
  SiteSetting.set(k, v) if SiteSetting.get(k) != v
rescue => e
  puts "  #{k}: #{e.message}"
end

# Staff-only writes: everyone can read, nobody but staff can post, like or sign up.
Discourse.enable_readonly_mode(Discourse::STAFF_WRITES_ONLY_MODE_KEY)
puts "readonly: #{Discourse.staff_writes_only_mode?}, topics #{Topic.count}, categories #{Category.count}"
