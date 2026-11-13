"""Small Tkinter calendar popup for selecting search-filter dates.

Uses only Python's standard library; the selected date is returned as ISO text.
"""
import calendar
from datetime import date
import tkinter as tk
from tkinter import ttk


def show_calendar(parent, anchor, date_variable, on_select):
    """Show a month calendar next to a date entry.

    Args:
        parent: Main Tkinter application.
        anchor: Widget used to position the calendar popup.
        date_variable: StringVar receiving a YYYY-MM-DD date.
        on_select: Callback to refresh search results after selection.
    """
    try:
        initial = date.fromisoformat(date_variable.get().strip())
    except ValueError:
        initial = date.today()

    popup = tk.Toplevel(parent)
    popup.title("Choose date")
    popup.resizable(False, False)
    popup.transient(parent)

    # Keep the popup near its date field and within the visible screen.
    popup.update_idletasks()
    x = min(anchor.winfo_rootx(), parent.winfo_screenwidth() - 280)
    y = anchor.winfo_rooty() + anchor.winfo_height() + 3
    popup.geometry(f"+{max(0, x)}+{max(0, y)}")

    month = initial.month
    year = initial.year
    container = ttk.Frame(popup, padding=10)
    container.pack()
    header = ttk.Frame(container)
    header.pack(fill="x")
    grid = ttk.Frame(container)
    grid.pack(pady=(8, 0))
    heading = ttk.Label(header, font=("Segoe UI", 11, "bold"), anchor="center")
    heading.pack(side="left", fill="x", expand=True)

    def draw():
        """Rebuild the day buttons for the currently displayed month."""
        heading.configure(text=f"{calendar.month_name[month]} {year}")
        for child in grid.winfo_children():
            child.destroy()
        for column, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            ttk.Label(grid, text=name, width=4, anchor="center").grid(
                row=0, column=column, padx=2, pady=3
            )
        for row, week in enumerate(calendar.monthcalendar(year, month), start=1):
            for column, day in enumerate(week):
                if not day:
                    continue
                ttk.Button(
                    grid, text=str(day), width=3,
                    command=lambda d=day: choose(d)
                ).grid(row=row, column=column, padx=2, pady=2)

    def navigate(change):
        nonlocal month, year
        month += change
        if month < 1:
            month, year = 12, year - 1
        elif month > 12:
            month, year = 1, year + 1
        draw()

    def choose(day):
        date_variable.set(date(year, month, day).isoformat())
        popup.destroy()
        on_select()

    ttk.Button(header, text="◀", width=3, command=lambda: navigate(-1)).pack(
        side="left", padx=(0, 8)
    )
    # Repack the title after the left arrow to maintain natural reading order.
    heading.pack_forget()
    heading.pack(side="left", fill="x", expand=True)
    ttk.Button(header, text="▶", width=3, command=lambda: navigate(1)).pack(
        side="right", padx=(8, 0)
    )
    ttk.Button(container, text="Today", command=lambda: (
        date_variable.set(date.today().isoformat()), popup.destroy(), on_select()
    )).pack(pady=(9, 0))
    popup.bind("<Escape>", lambda event: popup.destroy())
    draw()
    popup.focus_set()
