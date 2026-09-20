"""
Generates a downloadable PDF version of the Student Result Report using
reportlab. This gives a genuine "Download PDF" file (separate from the
browser's Print dialog, which can be unreliable on some devices).
"""
import os
from io import BytesIO

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image, HRFlowable
)

NAVY = colors.HexColor("#1b2a4e")
GRADE_COLOURS = {
    "A": colors.HexColor("#16a34a"),
    "B": colors.HexColor("#2563eb"),
    "C": colors.HexColor("#b45309"),
    "D": colors.HexColor("#ea580c"),
    "E": colors.HexColor("#dc2626"),
    "-": colors.HexColor("#6c757d"),
}


def _grade_colour(letter):
    return GRADE_COLOURS.get(letter, colors.black)


def _short_subject_label(name, max_len=12):
    """Header label for the class result grid. Most subject names fit as-is;
    a handful (e.g. "Science and Technology") are too long for a column
    shared across 6-10 subjects, so long ones are abbreviated to short
    connector-free word stems rather than being force-broken mid-word."""
    label = name.upper()
    if len(label) <= max_len:
        return label
    words = [w for w in label.split() if w not in ("AND", "OF", "THE", "&")]
    short = " ".join(w[:4] for w in words)
    return short[:max_len] if short else label[:max_len]


def build_student_report_pdf(data, logo_abs_path=None):
    """
    data keys required:
      school_name, academic_year, reg_no, full_name, class_name, gender,
      exam_type, subjects (list of {name, mark, grade}), total, average,
      overall_grade, remark, position, total_students, teacher_name, report_date
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=10 * mm, bottomMargin=10 * mm,
        leftMargin=10 * mm, rightMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle("kpsTitle", parent=styles["Title"], fontSize=18,
                                  textColor=NAVY, alignment=TA_CENTER, spaceAfter=2,
                                  fontName="Helvetica-Bold")
    subtitle_style = ParagraphStyle("kpsSubtitle", parent=styles["Normal"], fontSize=13,
                                     textColor=NAVY, alignment=TA_CENTER, spaceAfter=8,
                                     fontName="Helvetica-Bold")
    badge_style = ParagraphStyle("kpsBadge", parent=styles["Normal"], fontSize=10,
                                  textColor=colors.white, alignment=TA_CENTER,
                                  fontName="Helvetica-Bold")
    info_label_style = ParagraphStyle("kpsInfoLabel", parent=styles["Normal"], fontSize=9.5,
                                       alignment=TA_LEFT, fontName="Helvetica")

    # ---- Header: logo + school name + subtitle + badge ----
    header_cells = []
    logo_img = ""
    if logo_abs_path and os.path.exists(logo_abs_path):
        try:
            # Validate eagerly with PIL - reportlab's Image() only reads the
            # file lazily when the PDF is actually built, so a corrupt/invalid
            # upload would otherwise crash report generation at build time.
            from PIL import Image as PILImage
            with PILImage.open(logo_abs_path) as im:
                im.verify()
            logo_img = Image(logo_abs_path, width=20 * mm, height=20 * mm)
        except Exception:
            logo_img = ""

    badge_table = Table(
        [[Paragraph(f"ACADEMIC YEAR: {data['academic_year']}", badge_style)]],
        colWidths=[55 * mm],
    )
    badge_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    # Center the badge within the SAME column as the title/subtitle above it
    # (not the full page width) so it lines up perfectly under the heading,
    # regardless of the logo column's width.
    badge_table.hAlign = "CENTER"

    center_flow = [
        Paragraph(data["school_name"].upper(), title_style),
        Paragraph("RESULT REPORT", subtitle_style),
        Spacer(1, 4),
        badge_table,
    ]

    header_table = Table(
        [[logo_img, center_flow]],
        colWidths=[24 * mm, 166 * mm],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    # ---- Info panel: right-aligned, one field per line ----
    info_rows = [
        ("Adm No", data["reg_no"]),
        ("Name", data["full_name"]),
        ("Class", data["class_name"]),
        ("Gender", data["gender"]),
        ("Exam", data["exam_type"]),
    ]
    info_table_data = [
        [Paragraph(f"<b>{k}:</b> {v}", info_label_style)]
        for k, v in info_rows
    ]
    info_table = Table(info_table_data, colWidths=[90 * mm])
    info_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    right_wrap = Table([[info_table]], colWidths=[190 * mm])
    right_wrap.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "LEFT")]))
    story.append(right_wrap)
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1.4, color=NAVY))
    story.append(Spacer(1, 10))

    # ---- Subject table ----
    table_header_style = ParagraphStyle("kpsTh", parent=styles["Normal"], fontSize=9,
                                         textColor=colors.white, fontName="Helvetica-Bold",
                                         alignment=TA_CENTER)
    cell_style = ParagraphStyle("kpsTd", parent=styles["Normal"], fontSize=9.5)
    cell_center = ParagraphStyle("kpsTdC", parent=cell_style, alignment=TA_CENTER)

    rows = [[
        Paragraph("S/N", table_header_style),
        Paragraph("Subject", table_header_style),
        Paragraph("Mark (/100)", table_header_style),
        Paragraph("Grade", table_header_style),
    ]]
    for i, sub in enumerate(data["subjects"], start=1):
        grade_style = ParagraphStyle(
            f"grade{i}", parent=cell_center, textColor=_grade_colour(sub["grade"]),
            fontName="Helvetica-Bold",
        )
        rows.append([
            Paragraph(str(i), cell_center),
            Paragraph(sub["name"].upper(), cell_style),
            Paragraph(str(sub["mark"]) if sub["mark"] is not None else "-", cell_center),
            Paragraph(sub["grade"], grade_style),
        ])

    subj_table = Table(rows, colWidths=[14 * mm, 100 * mm, 38 * mm, 38 * mm], repeatRows=1)
    subj_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for r in range(1, len(rows)):
        if r % 2 == 0:
            subj_style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f7f8fb")))
    subj_table.setStyle(TableStyle(subj_style))
    story.append(subj_table)
    story.append(Spacer(1, 10))

    # ---- Summary table ----
    overall_style = ParagraphStyle(
        "kpsOverall", parent=cell_center, textColor=_grade_colour(data["overall_grade"]),
        fontName="Helvetica-Bold", fontSize=11,
    )
    summary_header = ["Total", "Average", "Overall Grade", "Position", "Remark"]
    summary_values = [
        str(data["total"]), str(data["average"]),
        Paragraph(data["overall_grade"], overall_style),
        f"{data['position']} of {data['total_students']}",
        str(data.get("remark") or "-"),
    ]
    summary_table = Table(
        [[Paragraph(h, table_header_style.clone("sumH", textColor=NAVY)) for h in summary_header],
         summary_values],
        colWidths=[38 * mm] * 5,
    )
    summary_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cccccc")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 11),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 12))

    # ---- Teacher's comment (blank box for hand-written remarks) ----
    comment_label_style = ParagraphStyle("commentLabel", parent=styles["Normal"], fontSize=9.5,
                                          textColor=NAVY, fontName="Helvetica-Bold")
    story.append(Paragraph("Teacher's Comment:", comment_label_style))
    story.append(Spacer(1, 3))
    blank_box = Table([[""]], colWidths=[190 * mm], rowHeights=[16 * mm])
    blank_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#cccccc")),
    ]))
    story.append(blank_box)
    story.append(Spacer(1, 22))

    # ---- Footer: signatures + stamp ----
    sign_label_style = ParagraphStyle("signLabel", parent=styles["Normal"], fontSize=9,
                                       alignment=TA_CENTER, fontName="Helvetica-Bold",
                                       textColor=NAVY)
    date_style = ParagraphStyle("dateLabel", parent=styles["Normal"], fontSize=8,
                                 alignment=TA_CENTER, textColor=colors.HexColor("#666666"))
    stamp_style = ParagraphStyle("stampLabel", parent=styles["Normal"], fontSize=7.5,
                                  alignment=TA_CENTER, textColor=colors.HexColor("#8891a8"))

    teacher_line = "CLASS TEACHER SIGN."
    if data.get("teacher_name"):
        teacher_line += f"<br/><font size=7>{data['teacher_name']}</font>"

    footer_data = [[
        Paragraph("...........................................", sign_label_style),
        "",
        Paragraph("...........................................", sign_label_style),
    ], [
        Paragraph(teacher_line, sign_label_style),
        Paragraph(f"{data['school_name'].upper()}<br/>OFFICIAL STAMP", stamp_style),
        Paragraph("HEAD MASTER SIGN.", sign_label_style),
    ], [
        Paragraph(f"Date: {data['report_date']}", date_style),
        "",
        Paragraph(f"Date: {data['report_date']}", date_style),
    ]]
    footer_table = Table(footer_data, colWidths=[63 * mm, 64 * mm, 63 * mm])
    footer_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(footer_table)

    doc.build(story)
    buf.seek(0)
    return buf.read()


def build_class_result_pdf(data, logo_abs_path=None):
    """
    Downloadable PDF of a whole class's result sheet (landscape, since a
    class can have 6-10+ subject columns) - the PDF counterpart of the
    on-screen/Print "Class Result Sheet" on the Review Results page.

    data keys required:
      school_name, academic_year, exam_type, class_name, teacher_name,
      class_average, class_grade, class_position, total_students, report_date,
      subjects (list of subject name strings, in column order),
      rows (list of {name, reg_no, scores (list, same order as subjects, each a
      number or None), total, average, grade, remark, position})
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        topMargin=10 * mm, bottomMargin=10 * mm,
        leftMargin=10 * mm, rightMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    story = []
    page_width = landscape(A4)[0] - 20 * mm  # usable width after margins

    title_style = ParagraphStyle("clsTitle", parent=styles["Title"], fontSize=17,
                                  textColor=NAVY, alignment=TA_CENTER, spaceAfter=2,
                                  fontName="Helvetica-Bold")
    subtitle_style = ParagraphStyle("clsSubtitle", parent=styles["Normal"], fontSize=12,
                                     textColor=NAVY, alignment=TA_CENTER, spaceAfter=6,
                                     fontName="Helvetica-Bold")
    badge_style = ParagraphStyle("clsBadge", parent=styles["Normal"], fontSize=10,
                                  textColor=colors.white, alignment=TA_CENTER,
                                  fontName="Helvetica-Bold")
    info_label_style = ParagraphStyle("clsInfoLabel", parent=styles["Normal"], fontSize=9.5,
                                       alignment=TA_LEFT, fontName="Helvetica")

    # ---- Header: logo + school name + subtitle + badge ----
    logo_img = ""
    if logo_abs_path and os.path.exists(logo_abs_path):
        try:
            from PIL import Image as PILImage
            with PILImage.open(logo_abs_path) as im:
                im.verify()
            logo_img = Image(logo_abs_path, width=18 * mm, height=18 * mm)
        except Exception:
            logo_img = ""

    badge_table = Table(
        [[Paragraph(f"{data['exam_type'].upper()} - {data['academic_year']}", badge_style)]],
        colWidths=[65 * mm],
    )
    badge_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    badge_table.hAlign = "CENTER"

    center_flow = [
        Paragraph(data["school_name"].upper(), title_style),
        Paragraph(f"CLASS RESULT SHEET - {data['class_name'].upper()}", subtitle_style),
        Spacer(1, 3),
        badge_table,
    ]
    header_table = Table(
        [[logo_img, center_flow]],
        colWidths=[22 * mm, page_width - 22 * mm],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 6))

    # ---- Info strip: Class Teacher / Students / Class Average+Grade ----
    overall_style = ParagraphStyle(
        "clsOverall", parent=info_label_style, textColor=_grade_colour(data["class_grade"]),
        fontName="Helvetica-Bold",
    )
    info_cells = [
        Paragraph(f"<b>Class Teacher:</b> {data.get('teacher_name') or '-'}", info_label_style),
        Paragraph(f"<b>Students:</b> {data['total_students']}", info_label_style),
        Paragraph(f"<b>Class Average:</b> {data['class_average']}", info_label_style),
        Paragraph(f"<b>Class Grade:</b> {data['class_grade']}", overall_style),
        Paragraph(f"<b>Class Position:</b> {data.get('class_position', '-')}", overall_style),
    ]
    info_table = Table([info_cells], colWidths=[page_width / 5.0] * 5)
    info_table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info_table)
    story.append(HRFlowable(width="100%", thickness=1.2, color=NAVY))
    story.append(Spacer(1, 8))

    # ---- Class table: S/N, Name, one column per subject, Total, Average, Grade, Position ----
    table_header_style = ParagraphStyle("clsTh", parent=styles["Normal"], fontSize=7.3,
                                         textColor=colors.white, fontName="Helvetica-Bold",
                                         alignment=TA_CENTER, leading=8.5)
    cell_style = ParagraphStyle("clsTd", parent=styles["Normal"], fontSize=8.5)
    cell_center = ParagraphStyle("clsTdC", parent=cell_style, alignment=TA_CENTER)

    subjects = data["subjects"]
    header_row = [
        Paragraph("S/N", table_header_style),
        Paragraph("REG NO", table_header_style),
        Paragraph("NAME", table_header_style),
    ] + [Paragraph(_short_subject_label(s), table_header_style) for s in subjects] + [
        Paragraph("TOTAL", table_header_style),
        Paragraph("AVERAGE", table_header_style),
        Paragraph("GRADE", table_header_style),
        Paragraph("POSITION", table_header_style),
        Paragraph("REMARK", table_header_style),
    ]
    rows = [header_row]
    for i, row in enumerate(data["rows"], start=1):
        grade_style = ParagraphStyle(
            f"clsGrade{i}", parent=cell_center, textColor=_grade_colour(row["grade"]),
            fontName="Helvetica-Bold",
        )
        rows.append(
            [Paragraph(str(i), cell_center), Paragraph(str(row.get("reg_no") or "-"), cell_center),
             Paragraph(row["name"].upper(), cell_style)]
            + [Paragraph(str(s) if s is not None else "-", cell_center) for s in row["scores"]]
            + [
                Paragraph(str(row["total"]) if row["total"] is not None else "-", cell_center),
                Paragraph(str(row["average"]) if row["average"] is not None else "-", cell_center),
                Paragraph(row["grade"], grade_style),
                Paragraph(str(row["position"]) if row["position"] is not None else "-", cell_center),
                Paragraph(row.get("remark") or "-", cell_center),
            ]
        )

    # Column widths: fixed columns first, subjects share the remaining
    # space evenly (clamped so the table never overflows the page, and
    # wide enough that header words like "AVERAGE"/"POSITION" or a long
    # subject name don't get force-broken mid-word).
    sn_w, reg_w, name_w, total_w, avg_w, grade_w, pos_w, remark_w = 9, 20, 26, 13, 18, 14, 17, 24
    fixed_w = sn_w + reg_w + name_w + total_w + avg_w + grade_w + pos_w + remark_w
    subject_area = max(0, (page_width / mm) - fixed_w)
    subj_w = max(18, min(28, subject_area / max(1, len(subjects))))
    col_widths = (
        [sn_w * mm, reg_w * mm, name_w * mm]
        + [subj_w * mm] * len(subjects)
        + [total_w * mm, avg_w * mm, grade_w * mm, pos_w * mm, remark_w * mm]
    )

    class_table = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl_style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for r in range(1, len(rows)):
        if r % 2 == 0:
            tbl_style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#f7f8fb")))
    class_table.setStyle(TableStyle(tbl_style))
    story.append(class_table)
    story.append(Spacer(1, 20))

    # ---- Footer: signatures + stamp (same convention as the student report) ----
    sign_label_style = ParagraphStyle("clsSignLabel", parent=styles["Normal"], fontSize=9,
                                       alignment=TA_CENTER, fontName="Helvetica-Bold",
                                       textColor=NAVY)
    date_style = ParagraphStyle("clsDateLabel", parent=styles["Normal"], fontSize=8,
                                 alignment=TA_CENTER, textColor=colors.HexColor("#666666"))
    stamp_style = ParagraphStyle("clsStampLabel", parent=styles["Normal"], fontSize=7.5,
                                  alignment=TA_CENTER, textColor=colors.HexColor("#8891a8"))

    teacher_line = "CLASS TEACHER SIGN."
    if data.get("teacher_name"):
        teacher_line += f"<br/><font size=7>{data['teacher_name']}</font>"

    footer_data = [[
        Paragraph("...........................................", sign_label_style),
        "",
        Paragraph("...........................................", sign_label_style),
    ], [
        Paragraph(teacher_line, sign_label_style),
        Paragraph(f"{data['school_name'].upper()}<br/>OFFICIAL STAMP", stamp_style),
        Paragraph("HEAD MASTER SIGN.", sign_label_style),
    ], [
        Paragraph(f"Date: {data['report_date']}", date_style),
        "",
        Paragraph(f"Date: {data['report_date']}", date_style),
    ]]
    third = page_width / 3.0
    footer_table = Table(footer_data, colWidths=[third, third, third])
    footer_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(footer_table)

    doc.build(story)
    buf.seek(0)
    return buf.read()
