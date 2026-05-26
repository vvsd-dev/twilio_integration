import frappe
from frappe import _
from frappe.email.doctype.notification.notification import Notification, get_context, json
from frappe.utils.print_format import download_pdf
from twilio_integration.twilio_integration.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage
from PyPDF2 import PdfMerger
import os
import io


class SendNotification(Notification):
	def validate(self):
		self.validate_twilio_settings()

	def validate_twilio_settings(self):
		if self.enabled and self.channel == "WhatsApp" \
			and not frappe.db.get_single_value("Twilio Settings", "enabled"):
			frappe.throw(_("Please enable Twilio settings to send WhatsApp messages"))

	def send(self, doc):
		context = get_context(doc)
		context = {"doc": doc, "alert": self, "comments": None}
		if doc.get("_comments"):
			context["comments"] = json.loads(doc.get("_comments"))

		if self.is_standard:
			self.load_standard_properties(context)

		try:
			if self.channel == 'WhatsApp':
				# Serialize doc data NOW while the document is guaranteed to exist,
				# so the async worker does not need to re-fetch it from the DB.
				frappe.enqueue(
					self.send_whatsapp_msg_async,
					queue='default',
					timeout=300,
					doctype=self.document_type,
					docname=doc.name,
					notification_name=self.name,
					doc_data=doc.as_dict()  # safeguard 1: pass data at enqueue time
				)
		except:
			frappe.log_error(title='Failed to send notification', message=frappe.get_traceback())

		super(SendNotification, self).send(doc)

	def send_whatsapp_msg_async(self, doctype, docname, notification_name, doc_data=None):
		try:
			notification = frappe.get_doc("Notification", notification_name)

			if doc_data:
				doc = frappe.get_doc({"doctype": doctype, **doc_data})
				doc.name = docname
			else:
				if not frappe.db.exists(doctype, docname):
					frappe.log_error(title="WhatsApp Notification Skipped",
						message=f"{doctype} '{docname}' no longer exists.\nNotification: {notification_name}")
					return
				doc = frappe.get_doc(doctype, docname)

			context = {"doc": doc, "alert": notification, "comments": None}
			if doc.get("_comments"):
				context["comments"] = json.loads(doc.get("_comments"))

			message = frappe.render_template(notification.message, context)
			receiver_list = notification.get_receiver_list(doc, context)

			# ── Generate & save PDF FIRST so we know the real filename ───────────
			attachments = None
			saved_file_url = None
			saved_filename = None

			if notification.attach_print:
				pdf_result = self.get_pdf_attachment(doc, notification.print_format, doctype)
				if pdf_result:
					# Save immediately to get the real filename (Frappe may add suffix)
					saved_file_url, saved_filename = WhatsAppMessage.save_attachment_early(
						pdf_result, doctype, doc.name
					)
					attachments = pdf_result  # still pass for backward compat

			# Inject real filename/url into context BEFORE variables are rendered
			context["pdf_file_url"] = saved_file_url or ""
			context["pdf_filename"] = saved_filename or f"{doc.name}.pdf"
			# ─────────────────────────────────────────────────────────────────────

			# Build content_variables
			content_variables = None
			if notification.get("variables"):
				content_variables = {}
				for row in notification.variables:
					try:
						rendered_value = frappe.render_template(row.variable_data, context)
						content_variables[str(row.variable_name)] = rendered_value
					except Exception as e:
						frappe.log_error(
							title=f"Failed to render variable '{row.variable_name}'",
							message=f"{str(e)}\n{frappe.get_traceback()}"
						)
						content_variables[str(row.variable_name)] = ""

			template_id = notification.get("whatsapp_template_id")

			whatsapp_params = {
				"receiver_list": receiver_list,
				"message": message,
				"doctype": doctype,
				"docname": doc.name,
				# Pass the already-known URL directly so handle_attachment
				# doesn't re-save and generate a new suffix
				"saved_file_url": saved_file_url,
				"attachments": attachments,
			}
			if template_id:
				whatsapp_params["template_id"] = template_id
			if content_variables:
				whatsapp_params["content_variables"] = json.dumps(content_variables)

			WhatsAppMessage.send_whatsapp_message(**whatsapp_params)

		except Exception as e:
			frappe.log_error(
				title='Failed to send WhatsApp async',
				message=f"{str(e)}\n{frappe.get_traceback()}"
			)

	def get_pdf_attachment(self, doc, print_format, doctype):
		"""Generate PDF attachment from print format and merge with existing merged PDF if available"""
		try:
			if not print_format:
				frappe.log_error(
					title="PDF Generation Warning",
					message=f"No print format specified for {doctype} {doc.name}"
				)
				return None

			# Generate PDF from print format - this returns bytes
			pdf_content = frappe.get_print(
				doctype=doctype,
				name=doc.name,
				print_format=print_format,
				as_pdf=True,
				doc=doc
			)

			# Check if PDF was generated
			if not pdf_content:
				frappe.log_error(
					title="PDF Generation Failed",
					message=f"frappe.get_print returned None for {doctype} {doc.name} with format {print_format}"
				)
				return None

			frappe.log_error(
				title="PDF Generated Successfully",
				message=f"PDF size: {len(pdf_content)} bytes for {doc.name} using format '{print_format}'"
			)

			# Check if merged PDF exists for this document
			merged_pdf_path = self.get_merged_pdf_path(doc.name, doctype)

			if merged_pdf_path:
				# Merge the print format PDF with the existing merged PDF
				try:
					final_pdf_content = self.merge_pdfs_with_print_format(pdf_content, merged_pdf_path, doc.name)

					frappe.log_error(
						title="PDFs Merged Successfully",
						message=f"Merged print format PDF with existing merged PDF for {doc.name}. Final size: {len(final_pdf_content)} bytes"
					)

					# Return merged attachment
					return [{
						"fname": f"{doc.name}_complete.pdf",
						"fcontent": final_pdf_content
					}]
				except Exception as e:
					frappe.log_error(
						title=f"Failed to merge PDFs for {doc.name}",
						message=f"Error: {str(e)}\n{frappe.get_traceback()}\nFalling back to print format PDF only"
					)
					# Fall back to just the print format PDF
					pass

			# Return attachment with just the print format PDF
			return [{
				"fname": f"{doc.name}.pdf",
				"fcontent": pdf_content
			}]

		except Exception as e:
			frappe.log_error(
				title=f"Failed to generate PDF for {doctype} {doc.name}",
				message=f"Print Format: {print_format}\nError: {str(e)}\n{frappe.get_traceback()}"
			)
			return None

	def get_merged_pdf_path(self, docname, doctype):
		"""Check if a merged PDF exists for this document"""
		try:
			# --- Safeguard 2: bail out early if the parent document no longer exists ---
			# This prevents misleading File queries when the doc was deleted between
			# enqueue time and the moment get_pdf_attachment calls this method.
			if not frappe.db.exists(doctype, docname):
				frappe.log_error(
					title="get_merged_pdf_path: Document Gone",
					message=(
						f"{doctype} '{docname}' no longer exists. "
						"Skipping merged-PDF lookup to avoid stale File queries."
					)
				)
				return None

			# Look for merged PDF file attached to the document
			merged_filename = f"{docname}_merged_attachments.pdf"

			# First, get all files attached to this document for debugging
			all_files = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": doctype,
					"attached_to_name": docname
				},
				fields=["name", "file_name", "file_url", "is_private"]
			)

			frappe.log_error(
				title="All Attached Files",
				message=f"Document: {docname}\nFiles found: {len(all_files)}\nFiles: {json.dumps(all_files, indent=2)}"
			)

			# Try to find merged PDF - check both exact match and partial match
			file_doc = None
			for f in all_files:
				if "merged" in f.get("file_name", "").lower():
					file_doc = f
					frappe.log_error(
						title="Found Merged PDF",
						message=f"Using file: {json.dumps(file_doc, indent=2)}"
					)
					break

			if not file_doc:
				frappe.log_error(
					title="Merged PDF Not Found",
					message=f"No merged PDF found for {docname}. Looking for filename containing 'merged'"
				)
				return None

			file_url = file_doc.get("file_url")
			is_private = file_doc.get("is_private", 0)

			# Construct file path
			site_path = frappe.get_site_path()

			if file_url.startswith('/'):
				file_url = file_url[1:]

			if file_url.startswith('files/'):
				file_path = os.path.join(site_path, 'public', file_url)
			elif file_url.startswith('private/files/'):
				file_path = os.path.join(site_path, file_url)
			else:
				filename = file_url.split('/')[-1]
				if is_private:
					file_path = os.path.join(site_path, 'private', 'files', filename)
				else:
					file_path = os.path.join(site_path, 'public', 'files', filename)

			# Check if file exists on disk
			if os.path.exists(file_path):
				frappe.log_error(
					title="✓ Merged PDF Found",
					message=f"Document: {docname}\nFile exists at: {file_path}\nFile size: {os.path.getsize(file_path)} bytes"
				)
				return file_path
			else:
				frappe.log_error(
					title="✗ Merged PDF File Missing",
					message=f"Document: {docname}\nFile record exists but file not found at: {file_path}\nChecked paths:\n- {file_path}"
				)
				return None

		except Exception as e:
			frappe.log_error(
				title=f"Error checking for merged PDF",
				message=f"Docname: {docname}\nDoctype: {doctype}\nError: {str(e)}\n{frappe.get_traceback()}"
			)
			return None

	def merge_pdfs_with_print_format(self, print_format_pdf_bytes, merged_pdf_path, docname):
		"""
		Merge print format PDF with existing merged PDF.
		Print format PDF will be first, followed by merged PDF.
		"""
		try:
			merger = PdfMerger()

			# Add print format PDF first (from bytes)
			print_format_pdf_file = io.BytesIO(print_format_pdf_bytes)
			merger.append(print_format_pdf_file)

			# Add existing merged PDF second (from file path)
			merger.append(merged_pdf_path)

			# Write to bytes buffer
			output_buffer = io.BytesIO()
			merger.write(output_buffer)
			merger.close()

			# Get the bytes
			final_pdf_bytes = output_buffer.getvalue()
			output_buffer.close()
			print_format_pdf_file.close()

			frappe.log_error(
				title="PDF Merge Details",
				message=(
					f"Document: {docname}\n"
					f"Print format PDF size: {len(print_format_pdf_bytes)} bytes\n"
					f"Merged PDF path: {merged_pdf_path}\n"
					f"Final size: {len(final_pdf_bytes)} bytes"
				)
			)

			return final_pdf_bytes

		except Exception as e:
			frappe.log_error(
				title=f"PDF Merge Failed for {docname}",
				message=f"Error: {str(e)}\n{frappe.get_traceback()}"
			)
			raise

	@staticmethod
	def merge_pdfs_with_pikepdf(print_format_pdf_bytes, merged_pdf_path, docname):
		"""
		Alternative merge method using pikepdf (more robust).
		Uncomment and use if you prefer pikepdf over PyPDF2.
		"""
		try:
			import pikepdf

			# Open print format PDF from bytes
			print_format_pdf = pikepdf.Pdf.open(io.BytesIO(print_format_pdf_bytes))

			# Open existing merged PDF from file
			merged_pdf = pikepdf.Pdf.open(merged_pdf_path)

			# Create new PDF with print format pages first
			final_pdf = pikepdf.Pdf.new()
			final_pdf.pages.extend(print_format_pdf.pages)
			final_pdf.pages.extend(merged_pdf.pages)

			# Write to bytes buffer
			output_buffer = io.BytesIO()
			final_pdf.save(output_buffer)

			# Get the bytes
			final_pdf_bytes = output_buffer.getvalue()

			# Clean up
			output_buffer.close()
			print_format_pdf.close()
			merged_pdf.close()

			return final_pdf_bytes

		except Exception as e:
			frappe.log_error(
				title=f"PDF Merge Failed (pikepdf) for {docname}",
				message=f"Error: {str(e)}\n{frappe.get_traceback()}"
			)
			raise