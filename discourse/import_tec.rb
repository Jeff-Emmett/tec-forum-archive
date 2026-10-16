# frozen_string_literal: true
#
# Import the TEC forum archive bundle (scraper/discourse_export.py) into a fresh Discourse.
#
#   su discourse -c 'RAILS_ENV=production bundle exec ruby /shared/import/import_tec.rb /shared/import'
#
# Keeps the forum's own topic ids, category ids and post numbers, so a forum.tecommons.org URL
# resolves to the same thing once the host is swapped. Post numbers that no web archive captured
# stay unused; the first number of each gap carries a staff note saying which posts are missing.
# Re-running is safe: users, categories, uploads and posts already imported are skipped.

require "/var/www/discourse/script/import_scripts/base"

# Discourse numbers posts max+1; during import take the number from the archive instead.
module TecPostNumber
  def next_post_number(topic_id, opts = {})
    want = Thread.current[:tec_post_number]
    return super unless want
    return want if opts[:post_type] == Post.types[:small_action]
    sets = ["highest_staff_post_number = GREATEST(highest_staff_post_number, :n)",
            "highest_post_number = GREATEST(highest_post_number, :n)"]
    sets << "reply_count = reply_count + 1" if opts[:reply]
    sets << "posts_count = posts_count + 1" if opts[:post]
    DB.exec("UPDATE topics SET #{sets.join(", ")} WHERE id = :t", n: want, t: topic_id)
    want
  end
end
Topic.singleton_class.prepend(TecPostNumber)

class ImportScripts::Tec < ImportScripts::Base
  ARCHIVE_URL = "https://tec-archive.mycostack.xyz"

  def initialize(dir)
    super()
    @dir = dir
    @bundle = JSON.parse(File.read(File.join(dir, "bundle.json")))
  end

  def get_site_settings_for_import
    super.merge(download_remote_images_to_local: false, max_oneboxes_per_post: 0)
  end

  def execute
    import_users
    import_categories
    import_uploads
    import_topics
    finish
  end

  def import_users
    puts "", "users"
    create_users(@bundle["users"]) do |u|
      {
        id: u["username"].downcase,
        username: u["username"],
        name: u["name"],
        email: "#{u["username"].downcase.gsub(/[^a-z0-9._-]/, "_")}@tec-archive.invalid",
        created_at: u["first_seen"],
        trust_level: 1,
        active: true,
        approved: true,
      }
    end
  end

  def user_id(username)
    return Discourse::SYSTEM_USER_ID if username.blank? || username == "system"
    user_id_from_imported_user_id(username.downcase) || Discourse::SYSTEM_USER_ID
  end

  def import_categories
    puts "", "categories"
    SiteSetting.max_category_nesting = 3 # Archive > Conviction Voting > Advice Process
    @bundle["categories"].each do |c|
      if c["id"] == SiteSetting.uncategorized_category_id
        cat = Category.find(c["id"])
        cat.update!(name: c["name"], color: c["color"] || cat.color,
                    description: c["description_text"])
        add_category(c["id"].to_s, cat)
        next
      end
      next if Category.exists?(c["id"])
      cat = Category.new(
        id: c["id"], name: c["name"], slug: c["slug"], user_id: Discourse::SYSTEM_USER_ID,
        parent_category_id: c["parent_category_id"], position: c["position"],
        color: c["color"] || "0088CC", text_color: "FFFFFF",
        description: c["description_text"],
      )
      # both: the accessor stops after_create, the custom field stops Category.ensure_consistency!
      # (a background job) from adding the definition topic later, which would take an archive id
      cat.skip_category_definition = true
      cat.custom_fields[Category::SKIP_DEFINITION_CUSTOM_FIELD] = true
      cat.custom_fields["import_id"] = c["id"].to_s
      cat.save!
      add_category(c["id"].to_s, cat)
    end
    DB.exec("SELECT setval('categories_id_seq', (SELECT MAX(id) FROM categories))")
  end

  def import_uploads
    puts "", "uploads"
    @upload_urls = {}
    store = File.join(@dir, "upload-map.json")
    @upload_urls = JSON.parse(File.read(store)) if File.exist?(store)
    @bundle["uploads"].each do |path, u|
      next if @upload_urls[path] && Upload.exists?(url: @upload_urls[path])
      up = create_upload(Discourse::SYSTEM_USER_ID, File.join(@dir, "uploads", u["file"]), u["name"])
      if up&.persisted?
        @upload_urls[path] = up.url
      else
        puts "  upload failed: #{path} #{up&.errors&.full_messages&.join(", ")}"
      end
    end
    File.write(store, JSON.generate(@upload_urls))
    puts "  #{@upload_urls.size} uploads"
  end

  def rewrite(raw)
    raw.gsub(%r{(src|href)="(/uploads/[^"]+)"}) do
      attr, path = $1, $2
      new = @upload_urls[path]
      new ? %(#{attr}="#{new}") : %(#{attr}="#{path}")
    end
  end

  def gap_note(a, b, slug, tid)
    what = a == b ? "Post #{a} of this topic was" : "Posts #{a}–#{b} of this topic were"
    "<p><em>#{what} not captured by any web archive, so #{a == b ? "it is" : "they are"} missing here. " \
      "Every recovered post and the capture it came from: <a href=\"#{ARCHIVE_URL}/t/#{slug}/#{tid}/\">evidence view</a>.</em></p>"
  end

  def import_topics
    puts "", "topics"
    topics = @bundle["topics"]
    total = topics.sum { |t| t["posts"].size + t["gaps"].size }
    done = 0
    start = Time.now
    topics.each do |t|
      tid = t["id"]
      items = t["posts"].map { |p| [p["post_number"], :post, p] } +
              t["gaps"].map { |a, b| [a, :gap, [a, b]] }
      items.sort_by!(&:first)
      # the exporter guarantees a captured opening post (post 1) for every topic it emits
      first = items.find { |n, kind, p| kind == :post && n == 1 && p["action_code"].nil? }
      if first.nil?
        puts "  topic #{tid}: no body post, skipped"
        next
      end
      items.each do |n, kind, p|
        done += 1
        import_id = "#{tid}/#{n}"
        next if post_id_from_imported_post_id(import_id)
        topic = Topic.find_by(id: tid)
        if topic.nil? && !(kind == :post && p.equal?(first[2]))
          next # cannot add replies before the opening post exists
        end
        opts = { created_at: (kind == :gap ? nil : p["created_at"]) }
        if kind == :gap
          prev = topic.posts.order(:post_number).last
          opts.merge!(user_id: Discourse::SYSTEM_USER_ID, topic_id: tid, post_type: Post.types[:moderator_action],
                      raw: gap_note(*p, t["slug"], tid), created_at: prev&.created_at || topic.created_at)
        elsif p["action_code"]
          opts.merge!(user_id: user_id(p["username"]), topic_id: tid, post_type: Post.types[:small_action],
                      action_code: p["action_code"], raw: "")
        else
          opts.merge!(user_id: user_id(p["username"]), raw: rewrite(p["raw"]),
                      custom_fields: { "tec_source" => p["source"].to_json })
          if topic
            opts[:topic_id] = tid
            opts[:reply_to_post_number] = p["reply_to_post_number"] if p["reply_to_post_number"]
          else
            DB.exec("SELECT setval('topics_id_seq', :id, false)", id: tid)
            opts.merge!(title: t["title"], category: t["category_id"] || SiteSetting.uncategorized_category_id,
                        meta_data: { "tec_captures" => t["captures"].to_json })
          end
        end
        Thread.current[:tec_post_number] = n
        post =
          begin
            create_post(opts, import_id)
          ensure
            Thread.current[:tec_post_number] = nil
          end
        if post.is_a?(Post)
          add_post(import_id, post)
          add_topic(post)
          if post.post_number != n || post.topic_id != tid
            raise "numbering drifted: wanted #{tid}/#{n}, got #{post.topic_id}/#{post.post_number}"
          end
          if p.is_a?(Hash) && p["like_count"].to_i > 0
            post.update_columns(like_count: p["like_count"].to_i)
          end
        else
          puts "  #{import_id}: #{post.inspect}"
        end
        print_status(done, total, start)
      end
      if (topic = Topic.find_by(id: tid))
        topic.update_columns(slug: t["slug"]) if t["slug"].present? && topic.slug != t["slug"]
        apply_states(topic, t["posts"])
      end
    end
    DB.exec("SELECT setval('topics_id_seq', (SELECT MAX(id) FROM topics))")
  end

  def apply_states(topic, posts)
    cols = {}
    posts.each do |p|
      case p["action_code"]
      when "pinned.enabled" then cols[:pinned_at] = p["created_at"]
      when "pinned.disabled" then cols[:pinned_at] = nil
      when "pinned_globally.enabled" then cols.merge!(pinned_at: p["created_at"], pinned_globally: true)
      when "pinned_globally.disabled" then cols.merge!(pinned_at: nil, pinned_globally: false)
      when "closed.enabled" then cols[:closed] = true
      when "closed.disabled" then cols[:closed] = false
      when "visible.disabled" then cols[:visible] = false
      when "visible.enabled" then cols[:visible] = true
      end
    end
    topic.update_columns(cols) if cols.any?
  end

  def finish
    puts "", "category definition topics"
    @bundle["categories"].each do |c|
      about = @bundle["topics"].find { |t| t["title"] == "About the #{c["name"]} category" && t["category_id"] == c["id"] }
      Category.where(id: c["id"]).update_all(topic_id: about["id"]) if about && Topic.exists?(about["id"])
    end

    puts "", "permalinks for topics no archive captured"
    @bundle["missing"].each do |m|
      ["t/#{m["slug"]}/#{m["id"]}", "t/#{m["id"]}"].each do |url|
        next if m["slug"].blank? && url.include?("//")
        Permalink.find_or_create_by!(url: url) { |pl| pl.external_url = m["wayback"] }
      rescue => e
        puts "  permalink #{url}: #{e.message}"
      end
    end

    puts "", "freeze"
    Topic.where(archetype: Archetype.default).where("id IN (?)", @bundle["topics"].map { |t| t["id"] }).update_all(archived: true)
  end

  # like_count is the archive's figure (who liked was never public); keep it through the
  # base class's counter resets.
  def perform
    super
    puts "", "restoring like counts"
    @bundle["topics"].each do |t|
      t["posts"].each do |p|
        next unless p["like_count"].to_i > 0
        Post.where(topic_id: t["id"], post_number: p["post_number"]).update_all(like_count: p["like_count"].to_i)
      end
    end
    DB.exec(<<~SQL)
      UPDATE topics SET like_count = s.n
      FROM (SELECT topic_id, SUM(like_count) n FROM posts GROUP BY topic_id) s
      WHERE s.topic_id = topics.id
    SQL
  end
end

ImportScripts::Tec.new(ARGV[0] || "/shared/import").perform
