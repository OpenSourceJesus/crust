/* OpenGL ES 3.1 for Crust -- the subset the packed-scene viewers use.
 *
 * Crust does not read /usr/include; like GLES2/gl2.h this is our own
 * declaration of Khronos's ABI, so tools/gles3_header_test.py compares
 * every constant (by value) and every prototype (by redeclaration) here
 * with Khronos's GLES3/gl31.h wherever that header can be found.
 *
 * Everything in ES 2.0 is ES 3.1 too: this includes GLES2/gl2.h and adds
 * what the viewers need beyond it -- vertex array objects, shader
 * storage buffers (SSBOs) for the packed handles `--gpu-handles` uploads
 * (engine_handles.h, shaders/handles.glsl), and the 2D batch path's texture
 * arrays, integer textures and instancing (gles3_batch.h).
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

/* The 2D batch path (gles3_batch.h): texture arrays, integer textures,
 * half-float and integer vertex attributes, instancing (ES 3.0) */
#define GL_TEXTURE_2D_ARRAY                   0x8C1A
#define GL_RGBA16UI                           0x8D76
#define GL_R8UI                               0x8232
#define GL_RGBA_INTEGER                       0x8D99
#define GL_RED_INTEGER                        0x8D94
#define GL_HALF_FLOAT                         0x140B

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
void glTexStorage3D(GLenum target, GLsizei levels, GLenum internalformat,
                    GLsizei width, GLsizei height, GLsizei depth);
void glTexSubImage3D(GLenum target, GLint level, GLint xoffset, GLint yoffset,
                     GLint zoffset, GLsizei width, GLsizei height, GLsizei depth,
                     GLenum format, GLenum type, const void *pixels);
void glVertexAttribIPointer(GLuint index, GLint size, GLenum type,
                            GLsizei stride, const void *pointer);
void glVertexAttribDivisor(GLuint index, GLuint divisor);
void glDrawArraysInstanced(GLenum mode, GLint first, GLsizei count,
                           GLsizei instancecount);
void glDispatchCompute(GLuint num_groups_x, GLuint num_groups_y,
                       GLuint num_groups_z);
void glMemoryBarrier(GLbitfield barriers);

#endif
