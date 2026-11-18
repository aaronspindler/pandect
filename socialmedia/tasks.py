from django.tasks import task


@task
def check_instagram_monitor(monitor_id, generation):
    from socialmedia.monitor_jobs import run_check

    return run_check(monitor_id, generation)
