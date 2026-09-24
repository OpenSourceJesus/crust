/* OpenGL ES 3.1 for Crust -- the subset the packed-scene viewers use.
 *
 * Crust does not read /usr/include; like GLES2/gl2.h this is our own
 * declaration of Khronos's ABI, so tools/gles3_header_test.py compares
 * every constant (by value) and every prototype (by redeclaration) here
 * with Khronos's GLES3/gl31.h wherever that header can be found.
 *
 * Everything in ES 2.0 is ES 3.1 too: this includes GLES2/gl2.h and adds
 * what the viewers need beyond it -- vertex array objects, and shader
 * storage buffers (SSBOs) for the packed handles `--gpu-handles` uploads
 * (engine_handles.h, shaders/handles.glsl).
 */
#ifndef _GLES3_GL31_H
#define _GLES3_GL31_H

#include <GLES2/gl2.h>

/* Queries */
#define GL_MAJOR_VERSION                      0x821B
#define GL_MINOR_VERSION                      0x821C

/* Vertex array objects (ES 3.0) */
#define GL_VERTEX_ARRAY_BINDING               0x85B5

/* Sized formats (ES 3.0) */
#define GL_RGBA8                              0x8058

/* Shader storage buffers and compute (ES 3.1) */
#define GL_SHADER_STORAGE_BUFFER              0x90D2
#define GL_SHADER_STORAGE_BUFFER_BINDING      0x90D3
#define GL_MAX_SHADER_STORAGE_BUFFER_BINDINGS 0x90DD
#define GL_SHADER_STORAGE_BARRIER_BIT         0x00002000
#define GL_COMPUTE_SHADER                     0x91B9

void glGenVertexArrays(GLsizei n, GLuint *arrays);
void glBindVertexArray(GLuint array);
void glDeleteVertexArrays(GLsizei n, const GLuint *arrays);
void glBindBufferBase(GLenum target, GLuint index, GLuint buffer);
void glDispatchCompute(GLuint num_groups_x, GLuint num_groups_y,
                       GLuint num_groups_z);
void glMemoryBarrier(GLbitfield barriers);

#endif
